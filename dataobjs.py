import re
import logging
from datetime import datetime

PchumLog = logging.getLogger("pchumLogger")
try:
    from PyQt6 import QtGui
except ImportError:
    print("PyQt5 fallback (dataobjs.py)")
    from PyQt5 import QtGui

from mood import Mood
from parsetools import (
    timeDifference,
    convertTags,
)


class PesterProfile:
    def __init__(
        self,
        handle,
        color=None,
        mood=Mood("offline"),
        group=None,
        notes="",
        chumdb=None,
    ):
        self.handle = handle
        if color is None:
            if chumdb:
                color = chumdb.getColor(handle, QtGui.QColor("black"))
            else:
                color = QtGui.QColor("black")
        self.color = color
        self.mood = mood
        if group is None:
            if chumdb:
                group = chumdb.getGroup(handle, "Chums")
            else:
                group = "Chums"
        self.group = group
        self.notes = notes

    def initials(self, time=None):
        handle = self.handle
        caps = [l for l in handle if l.isupper()]
        if not caps:
            caps = [""]
        PchumLog.debug("handle = %s", handle)
        PchumLog.debug("caps = %s", caps)
        # Fallback for invalid string
        try:
            initials = (handle[0] + caps[0]).upper()
        except:
            PchumLog.exception("")
            initials = "XX"
        PchumLog.debug("initials = %s", initials)
        if hasattr(self, "time"):
            if time:
                if self.time > time:
                    return "F" + initials
                elif self.time < time:
                    return "P" + initials
                else:
                    return "C" + initials
            else:
                return initials
        else:
            return initials

    def colorhtml(self):
        if self.color:
            return self.color.name()
        else:
            return "#000000"

    def colorcmd(self):
        if self.color:
            (r, g, b, _a) = self.color.getRgb()
            return "%d,%d,%d" % (r, g, b)
        else:
            return "0,0,0"

    def plaindict(self):
        return (
            self.handle,
            {
                "handle": self.handle,
                "mood": self.mood.name(),
                "color": str(self.color.name()),
                "group": str(self.group),
                "notes": str(self.notes),
            },
        )

    def blocked(self, config):
        return self.handle in config.getBlocklist()

    def memsg(self, syscolor, lexmsg, time=None):
        suffix = lexmsg[0].suffix
        msg = convertTags(lexmsg[1:], "text")
        uppersuffix = suffix.upper()
        if time is not None:
            handle = f"{time.temporal} {self.handle}"
            initials = time.pcf + self.initials() + time.number + uppersuffix
        else:
            handle = self.handle
            initials = self.initials() + uppersuffix
        return "<c={}>-- {}{} <c={}>[{}]</c> {} --</c>".format(
            syscolor.name(),
            handle,
            suffix,
            self.colorhtml(),
            initials,
            msg,
        )

    def pestermsg(self, otherchum, syscolor, verb):
        return "<c={}>-- {} <c={}>[{}]</c> {} {} <c={}>[{}]</c> at {} --</c>".format(
            syscolor.name(),
            self.handle,
            self.colorhtml(),
            self.initials(),
            verb,
            otherchum.handle,
            otherchum.colorhtml(),
            otherchum.initials(),
            datetime.now().strftime("%H:%M"),
        )

    def moodmsg(self, mood, syscolor, theme):
        return (
            '<c=%s>-- %s <c=%s>[%s]</c> changed their mood to %s <img src="%s" /> --</c>'
            % (
                syscolor.name(),
                self.handle,
                self.colorhtml(),
                self.initials(),
                mood.name().upper(),
                theme["main/chums/moods"][mood.name()]["icon"].replace(" ", "%20"),
            )
        )

    def idlemsg(self, syscolor, verb):
        return "<c={}>-- {} <c={}>[{}]</c> {} --</c>".format(
            syscolor.name(),
            self.handle,
            self.colorhtml(),
            self.initials(),
            verb,
        )

    def memoclosemsg(self, syscolor, initials, verb):
        if isinstance(initials, list):
            return "<c={}><c={}>{}</c> {}.</c>".format(
                syscolor.name(),
                self.colorhtml(),
                ", ".join(initials),
                verb,
            )
        return "<c={}><c={}>{}{}{}</c> {}.</c>".format(
            syscolor.name(),
            self.colorhtml(),
            initials.pcf,
            self.initials(),
            initials.number,
            verb,
        )

    def memonetsplitmsg(self, syscolor, initials):
        if len(initials) <= 0:
            return "<c=%s>Netsplit quits: <c=black>None</c></c>" % (syscolor.name())
        else:
            return "<c={}>Netsplit quits: <c=black>{}</c></c>".format(
                syscolor.name(),
                ", ".join(initials),
            )

    def memoopenmsg(self, syscolor, td, timeGrammar, verb, channel):
        """timeGrammar.temporal and timeGrammar.when are unused"""
        timetext = timeDifference(td)
        PchumLog.debug("pre pcf+self.initials()")
        initials = timeGrammar.pcf + self.initials()
        PchumLog.debug("post pcf+self.initials()")
        return "<c={}><c={}>{}</c> {} {} {}.</c>".format(
            syscolor.name(),
            self.colorhtml(),
            initials,
            timetext,
            verb,
            channel[1:].upper().replace("_", " "),
        )

    def memobanmsg(self, opchum, opgrammar, syscolor, initials, reason):
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        if isinstance(initials, list):
            if opchum.handle == reason:
                return (
                    "<c={}>{}</c> banned <c={}>{}</c> from responding to memo.".format(
                        opchum.colorhtml(),
                        opinit,
                        self.colorhtml(),
                        ", ".join(initials),
                    )
                )
            else:
                return (
                    "<c=%s>%s</c> banned <c=%s>%s</c> from responding to memo: <c=black>[%s]</c>."
                    % (
                        opchum.colorhtml(),
                        opinit,
                        self.colorhtml(),
                        ", ".join(initials),
                        reason,
                    )
                )
        else:
            PchumLog.exception("")
            initials = self.initials()
            if opchum.handle == reason:
                return "<c=%s>%s</c> banned <c=%s>%s</c> from responding to memo." % (
                    opchum.colorhtml(),
                    opinit,
                    self.colorhtml(),
                    initials,
                )
            else:
                return (
                    "<c=%s>%s</c> banned <c=%s>%s</c> from responding to memo: <c=black>[%s]</c>."
                    % (
                        opchum.colorhtml(),
                        opinit,
                        self.colorhtml(),
                        initials,
                        reason,
                    )
                )

    # As far as I'm aware, there's no IRC reply for this, this seems impossible to check for in practice.
    def memopermabanmsg(self, opchum, opgrammar, syscolor, timeGrammar):
        initials = timeGrammar.pcf + self.initials() + timeGrammar.number
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        return "<c={}>{}</c> permabanned <c={}>{}</c> from the memo.".format(
            opchum.colorhtml(),
            opinit,
            self.colorhtml(),
            initials,
        )

    def memojoinmsg(self, syscolor, td, timeGrammar, verb):
        # (temporal, pcf, when) = (timeGrammar.temporal, timeGrammar.pcf, timeGrammar.when)
        timetext = timeDifference(td)
        initials = timeGrammar.pcf + self.initials() + timeGrammar.number
        return "<c={}><c={}>{} {} [{}]</c> {} {}.</c>".format(
            syscolor.name(),
            self.colorhtml(),
            timeGrammar.temporal,
            self.handle,
            initials,
            timetext,
            verb,
        )

    def memoopmsg(self, opchum, opgrammar, syscolor):
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        return "<c={}>{}</c> made <c={}>{}</c> an OP.".format(
            opchum.colorhtml(),
            opinit,
            self.colorhtml(),
            self.initials(),
        )

    def memodeopmsg(self, opchum, opgrammar, syscolor):
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        return "<c={}>{}</c> took away <c={}>{}</c>'s OP powers.".format(
            opchum.colorhtml(),
            opinit,
            self.colorhtml(),
            self.initials(),
        )

    def memovoicemsg(self, opchum, opgrammar, syscolor):
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        return "<c={}>{}</c> gave <c={}>{}</c> voice.".format(
            opchum.colorhtml(),
            opinit,
            self.colorhtml(),
            self.initials(),
        )

    def memodevoicemsg(self, opchum, opgrammar, syscolor):
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        return "<c={}>{}</c> took away <c={}>{}</c>'s voice.".format(
            opchum.colorhtml(),
            opinit,
            self.colorhtml(),
            self.initials(),
        )

    def memomodemsg(self, opchum, opgrammar, syscolor, modeverb, modeon):
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        if modeon:
            modeon = "now"
        else:
            modeon = "no longer"
        return "<c={}>Memo is {} <c=black>{}</c> by <c={}>{}</c></c>".format(
            syscolor.name(),
            modeon,
            modeverb,
            opchum.colorhtml(),
            opinit,
        )

    def memoquirkkillmsg(self, opchum, opgrammar, syscolor):
        opinit = opgrammar.pcf + opchum.initials() + opgrammar.number
        return "<c={}><c={}>{}</c> turned off your quirk.</c>".format(
            syscolor.name(),
            opchum.colorhtml(),
            opinit,
        )

    @staticmethod
    def checkLength(handle):
        return len(handle) <= 256

    @staticmethod
    def checkValid(handle):
        caps = [l for l in handle if l.isupper()]
        if len(caps) != 1:
            return (False, "Must have exactly 1 uppercase letter")
        if handle[0].isupper():
            return (False, "Cannot start with uppercase letter")
        if re.search("[^A-Za-z0-9]", handle) is not None:
            return (False, "Only alphanumeric characters allowed")
        if handle[0].isnumeric():  # IRC doesn't allow this
            return (False, "Handles may not start with a number")
        return (True,)


class PesterHistory:
    def __init__(self):
        self.history = []
        self.current = 0
        self.saved = None

    def next(self, text):
        if self.current == 0:
            return None
        if self.current == len(self.history):
            self.save(text)
        self.current -= 1
        text = self.history[self.current]
        return text

    def prev(self):
        self.current += 1
        if self.current >= len(self.history):
            self.current = len(self.history)
            return self.retrieve()
        return self.history[self.current]

    def reset(self):
        self.current = len(self.history)
        self.saved = None

    def save(self, text):
        self.saved = text

    def retrieve(self):
        return self.saved

    def add(self, text):
        if len(self.history) == 0 or text != self.history[len(self.history) - 1]:
            self.history.append(text)
        self.reset()
