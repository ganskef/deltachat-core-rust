import time
import os
from queue import Queue

import deltachat


def test_db_busy_error(acfactory, tmpdir):
    starttime = time.time()

    def log(string):
        print("%3.2f %s" % (time.time() - starttime, string))

    # make a number of accounts
    accounts = acfactory.get_many_online_accounts(5, quiet=False)
    log("created %s accounts" % len(accounts))

    # put a bigfile into each account
    for acc in accounts:
        acc.bigfile = os.path.join(acc.get_blobdir(), "bigfile")
        with open(acc.bigfile, "wb") as f:
            f.write(b"01234567890"*1000_000)
    log("created %s bigfiles" % len(accounts))

    contact_addrs = [acc.get_self_contact().addr for acc in accounts]
    chat = accounts[0].create_group_chat("stress-group")
    for addr in contact_addrs[1:]:
        chat.add_contact(chat.account.create_contact(addr))

    # setup auto-responder bots which report back failures/actions
    report_queue = Queue()

    def report_func(replier, report_type, *report_args):
        report_queue.put((replier, report_type, report_args))

    # each replier receives all events and sends report events to receive_queue
    repliers = []
    for acc in accounts:
        replier = AutoReplier(acc, num_send=1000, num_bigfiles=0, report_func=report_func)
        acc.add_account_plugin(replier)
        repliers.append(replier)

    # kick off message sending
    # after which repliers will reply to each other
    chat.send_text("hello")

    alive_count = len(accounts)
    while alive_count > 0:
        replier, report_type, report_args = report_queue.get(10)
        addr = replier.account.get_self_contact().addr
        assert addr
        if report_type == ReportType.exit:
            alive_count -= 1
            log("{} EXIT -- remaining: {}".format(addr, alive_count))
            replier.account.shutdown(wait=True)
        elif report_type == ReportType.message_sent:
            log("{} sent message: {}".format(addr, report_args[0].text))
        elif report_type == ReportType.message_incoming:
            log("{} incoming message: {}".format(addr, report_args[0].text))
        elif report_type == ReportType.ffi_error:
            log("{} ERROR: {}".format(addr, report_args[0]))
            replier.account.shutdown(wait=True)
            alive_count -= 1


class ReportType:
    exit = "exit"
    message_sent = "message-sent"
    ffi_error = "ffi-error"
    message_incoming = "message-incoming"


class AutoReplier:
    def __init__(self, account, report_func, num_send, num_bigfiles):
        self.account = account
        self.report_func = report_func
        self.num_send = num_send
        self.num_bigfiles = num_bigfiles
        self.current_sent = 0

    @deltachat.account_hookimpl
    def ac_incoming_message(self, message):
        if self.current_sent >= self.num_send:
            return
        message.accept_sender_contact()
        message.mark_seen()
        self.report_func(self, ReportType.message_incoming, message)

        self.current_sent += 1
        # we are still alive, let's send a reply
        if self.num_bigfiles and self.current_sent % self.num_bigfiles == 0:
            message.chat.send_text("send big file as reply to: {}".format(message.text))
            msg = message.chat.send_file(self.account.bigfile)
        else:
            msg = message.chat.send_text("got message id {}, small text reply".format(message.id))
            assert msg.text
        self.report_func(self, ReportType.message_sent, msg)
        if self.current_sent >= self.num_send:
            self.report_func(self, ReportType.exit)
            return

    @deltachat.account_hookimpl
    def ac_process_ffi_event(self, ffi_event):
        if ffi_event.name == "DC_EVENT_ERROR":
            self.report_func(self, ReportType.ffi_error, ffi_event)