"""Provides classes and functions for communicating with an IRC server.

References for the IRC protocol:
 - Original IRC protocol specification: https://www.rfc-editor.org/rfc/rfc1459
 - Updated IRC Client specification: https://www.rfc-editor.org/rfc/rfc2812
 - Modern IRC client protocol writeup: https://modern.ircdocs.horse
 - IRCv3 protocol additions: https://ircv3.net/irc/
 - Draft of metadata specification: https://gist.github.com/k4bek4be/92c2937cefd49990fbebd001faf2b237
"""
import sys
import socket
import ssl
import random
import datetime
import logging

try:
    from PyQt6 import QtCore, QtGui
except ImportError:
    print("PyQt5 fallback (irc.py)")
    from PyQt5 import QtCore, QtGui

from mood import Mood
from dataobjs import PesterProfile
from generic import PesterList
from version import _pcVersion

PchumLog = logging.getLogger("pchumLogger")
SERVICES = [
    "nickserv",
    "chanserv",
    "memoserv",
    "operserv",
    "helpserv",
    "hostserv",
    "botserv",
]


class CommandError(Exception):
    def __init__(self, cmd):
        self.cmd = cmd


class NoSuchCommandError(CommandError):
    def __str__(self):
        return 'No such command "%s"' % ".".join(self.cmd)


class ProtectedCommandError(CommandError):
    def __str__(self):
        return 'Command "%s" is protected' % ".".join(self.cmd)


# Python 3
QString = str

try:
    import certifi
except ImportError:
    if sys.platform == "darwin":
        # Certifi is required to validate certificates on MacOS with pyinstaller builds.
        PchumLog.warning(
            "Failed to import certifi, which is recommended on MacOS. "
            "Pesterchum might not be able to validate certificates unless "
            "Python's root certs are installed."
        )
    else:
        PchumLog.info(
            "Failed to import certifi, Pesterchum will not be able to validate "
            "certificates if the system-provided root certificates are invalid."
        )


class IRCClientError(Exception):
    pass


class PesterIRC(QtCore.QThread):
    def __init__(self, config, window, verify_hostname=True):
        QtCore.QThread.__init__(self)
        self.mainwindow = window
        self.config = config
        self.unresponsive = False
        self.registeredIRC = False
        self.verify_hostname = verify_hostname
        self.metadata_supported = False
        self.stopIRC = None

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.host = self.config.server()
        self.port = self.config.port()
        self.ssl = self.config.ssl()
        self._end = False

        self.send_irc = SendIRC()

        self.conn = None
        
        self.joined = False
        self.channelnames = {}
        self.channel_list = []
        self.channel_field = None

        self.commands = {
            "001": self.welcome,
            "005": self.featurelist,
            "221": self.umodeis,
            "321": self.liststart,
            "322": self.list,
            "323": self.listend,
            "324": self.channelmodeis,
            "353": self.namreply,
            "366": self.endofnames,
            "432": self.erroneusnickname,
            "433": self.nicknameinuse,
            "436": self.nickcollision,
            "448": self.forbiddenchannel,  # non-standard
            "473": self.inviteonlychan,
            "761": self.keyvalue,  # 7XX is ircv3 deprecated metadata spec
            "766": self.nomatchingkey,
            "768": self.keynotset,
            "769": self.keynopermission,
            "770": self.metadatasubok,
            "error": self.error,
            "join": self.join,
            "kick": self.kick,
            "mode": self.mode,
            "part": self.part,
            "ping": self.ping,
            "privmsg": self.privmsg,
            "notice": self.notice,
            "quit": self.quit,
            "invite": self.invite,
            "nick": self.nick,  # We can get svsnicked
            "metadata": self.metadata,  # Metadata specification
            "tagmsg": self.tagmsg,  # IRCv3 message tags extension
            "cap": self.cap,  # IRCv3 Client Capability Negotiation
        }

    def get_ssl_context(self):
        """Returns an SSL context for connecting over SSL/TLS.
        Loads the certifi root certificate bundle if the certifi module is less
        than a year old or if the system certificate store is empty.

        The cert store on Windows also seems to have issues, so it's better
        to use the certifi provided bundle assuming it's a recent version.

        On MacOS the system cert store is usually empty, as Python does not use
        the system provided ones, instead relying on a bundle installed with the
        python installer."""
        default_context = ssl.create_default_context()
        if "certifi" not in sys.modules:
            return default_context

        # Get age of certifi module
        certifi_date = datetime.datetime.strptime(certifi.__version__, "%Y.%m.%d")
        current_date = datetime.datetime.now()
        certifi_age = current_date - certifi_date

        empty_cert_store = (
            list(default_context.cert_store_stats().values()).count(0) == 3
        )
        # 31557600 seconds is approximately 1 year
        if empty_cert_store or certifi_age.total_seconds() <= 31557600:
            PchumLog.info(
                "Using SSL/TLS context with certifi-provided root certificates."
            )
            return ssl.create_default_context(cafile=certifi.where())
        PchumLog.info("Using SSL/TLS context with system-provided root certificates.")
        return default_context

    def connect(self, verify_hostname=True):
        """initiates the connection to the server set in self.host:self.port
        self.ssl decides whether the connection uses ssl.

        Certificate validation when using SSL/TLS may be disabled by
        passing the 'verify_hostname' parameter. The user is asked if they
        want to disable it if this functions raises a certificate validation error,
        in which case the function may be called again with 'verify_hostname'."""
        PchumLog.info("connecting to {}:{}".format(self.host, self.port))

        # Open connection
        plaintext_socket = socket.create_connection((self.host, self.port))

        if self.ssl:
            # Upgrade connection to use SSL/TLS if enabled
            context = self.get_ssl_context()
            context.check_hostname = verify_hostname
            self.socket = context.wrap_socket(
                plaintext_socket, server_hostname=self.host
            )
        else:
            # SSL/TLS is disabled, connection is plaintext
            self.socket = plaintext_socket

        self.socket.settimeout(90)
        self.send_irc.socket = self.socket

        self.send_irc.nick(self.mainwindow.profile().handle)
        self.send_irc.user("pcc31", "pcc31")
        # if self.connect_cb:
        #    self.connect_cb(self)

    def conn_generator(self):
        """returns a generator object."""
        try:
            buffer = b""
            while not self._end:
                try:
                    buffer += self.socket.recv(1024)
                except OSError as e:
                    PchumLog.warning("conn exception {} in {}".format(e, self))
                    if self._end:
                        break
                    raise e
                else:
                    if self._end:
                        break

                    split_buffer = buffer.split(b"\r\n")
                    buffer = b""
                    if split_buffer[-1]:
                        # Incomplete line, add it back to the buffer.
                        buffer = split_buffer.pop()

                    for line in split_buffer:
                        line = line.decode(encoding="utf-8", errors="replace")
                        tags, prefix, command, args = self.parse_irc_line(line)
                        if command:
                            # Only need tags with tagmsg
                            if command.casefold() == "tagmsg":
                                self.run_command(command, prefix, tags, *args)
                            else:
                                self.run_command(command, prefix, *args)

                yield True
        except socket.timeout as se:
            PchumLog.debug("passing timeout")
            raise se
        except (OSError, ssl.SSLEOFError) as se:
            PchumLog.debug("problem: %s" % (str(se)))
            if self.socket:
                PchumLog.info("error: closing socket")
                self.socket.close()
            raise se
        except Exception as e:
            PchumLog.exception("Non-socket exception in conn_generator().")
            raise e
        else:
            PchumLog.debug("ending while, end is %s" % self._end)
            if self.socket:
                PchumLog.info("finished: closing socket")
                self.socket.close()
            yield False

    def parse_irc_line(self, line: str):
        parts = line.split(" ")
        tags = None
        prefix = None
        if parts[0].startswith(":"):
            prefix = parts[0][1:]
            command = parts[1]
            args = parts[2:]
        elif parts[0].startswith("@"):
            tags = parts[0]  # IRCv3 message tag
            prefix = parts[1][1:]
            command = parts[2]
            args = parts[3:]
        else:
            command = parts[0]
            args = parts[1:]
        command = command.casefold()

        # If ':' is present the subsequent args are one parameter.
        fused_args = []
        for idx, arg in enumerate(args):
            if arg.startswith(":"):
                final_param = " ".join(args[idx:])
                fused_args.append(final_param[1:])
                break
            else:
                fused_args.append(arg)

        return (tags, prefix, command, fused_args)

    def close(self):
        # with extreme prejudice
        if self.socket:
            PchumLog.info("shutdown socket")
            # print("shutdown socket")
            self._end = True
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                PchumLog.debug(
                    "Error while shutting down socket, already broken? %s" % str(e)
                )
            try:
                self.socket.close()
            except OSError as e:
                PchumLog.debug(
                    "Error while closing socket, already broken? %s" % str(e)
                )

    def IRCConnect(self):
        try:
            self.connect(self.verify_hostname)
        except ssl.SSLCertVerificationError as e:
            # Ask if users wants to connect anyway
            self.askToConnect.emit(e)
            raise e
        self.conn = self.conn_generator()

    def run(self):
        try:
            self.IRCConnect()
        except OSError as se:
            self.stopIRC = se
            return
        while True:
            res = True
            try:
                PchumLog.debug("updateIRC()")
                self.mainwindow.sincerecv = 0
                res = self.updateIRC()
            except socket.timeout as se:
                PchumLog.debug("timeout in thread %s" % (self))
                self.close()
                self.stopIRC = "{}, {}".format(type(se), se)
                return
            except (OSError, IndexError, ValueError) as se:
                self.stopIRC = "{}, {}".format(type(se), se)
                PchumLog.debug("socket error, exiting thread")
                return
            else:
                if not res:
                    PchumLog.debug("false Yield: %s, returning" % res)
                    return

    def setConnected(self):
        self.registeredIRC = True
        self.connected.emit()

    def setConnectionBroken(self):
        PchumLog.critical("setconnection broken")
        self.disconnectIRC()
        # self.brokenConnection = True  # Unused

    @QtCore.pyqtSlot()
    def updateIRC(self):
        try:
            res = next(self.conn)
        except socket.timeout as se:
            if self.registeredIRC:
                return True
            else:
                raise se
        except OSError as se:
            raise se
        except (OSError, ValueError, IndexError) as se:
            raise se
        except StopIteration:
            self.conn = self.conn_generator()
            return True
        else:
            return res

    @QtCore.pyqtSlot(PesterProfile)
    def getMood(self, *chums):
        """Get mood via metadata if supported"""

        # Get via metadata or via legacy method
        if self.metadata_supported:
            # Metadata
            for chum in chums:
                try:
                    self.send_irc.metadata(chum.handle, "get", "mood")
                except OSError as e:
                    PchumLog.warning(e)
                    self.setConnectionBroken()
        else:
            # Legacy
            PchumLog.warning(
                "Server doesn't seem to support metadata, using legacy GETMOOD."
            )
            chumglub = "GETMOOD "
            for chum in chums:
                if len(chumglub + chum.handle) >= 350:
                    try:
                        self.send_irc.privmsg("#pesterchum", chumglub)
                    except OSError as e:
                        PchumLog.warning(e)
                        self.setConnectionBroken()
                    chumglub = "GETMOOD "
                # No point in GETMOOD-ing services
                if chum.handle.casefold() not in SERVICES:
                    chumglub += chum.handle
            if chumglub != "GETMOOD ":
                try:
                    self.send_irc.privmsg("#pesterchum", chumglub)
                except OSError as e:
                    PchumLog.warning(e)
                    self.setConnectionBroken()

    @QtCore.pyqtSlot(PesterList)
    def getMoods(self, chums):
        self.getMood(*chums)

    @QtCore.pyqtSlot(QString, QString)
    def sendNotice(self, text, handle):
        self.send_irc.notice(handle, text)

    @QtCore.pyqtSlot(QString, QString)
    def sendMessage(self, text, handle):
        h = str(handle)
        textl = [str(text)]

        def splittext(l):
            if len(l[0]) > 450:
                space = l[0].rfind(" ", 0, 430)
                if space == -1:
                    space = 450
                elif l[0][space + 1 : space + 5] == "</c>":
                    space = space + 4
                a = l[0][0 : space + 1]
                b = l[0][space + 1 :]
                if a.count("<c") > a.count("</c>"):
                    # oh god ctags will break!! D=
                    hanging = []
                    usedends = []
                    c = a.rfind("<c")
                    while c != -1:
                        d = a.find("</c>", c)
                        while d in usedends:
                            d = a.find("</c>", d + 1)
                        if d != -1:
                            usedends.append(d)
                        else:
                            f = a.find(">", c) + 1
                            hanging.append(a[c:f])
                        c = a.rfind("<c", 0, c)

                    # end all ctags in first part
                    for _ in range(a.count("<c") - a.count("</c>")):
                        a = a + "</c>"
                    # start them up again in the second part
                    for c in hanging:
                        b = c + b
                if len(b) > 0:
                    return [a] + splittext([b])
                else:
                    return [a]
            else:
                return l

        textl = splittext(textl)
        try:
            for t in textl:
                self.send_irc.privmsg(h, t)
        except OSError as e:
            PchumLog.warning(e)
            self.setConnectionBroken()

    @QtCore.pyqtSlot(
        QString,
        QString,
    )
    
    def sendCTCP(self, handle, text):
        self.send_irc.ctcp(handle, text)

    @QtCore.pyqtSlot(QString, bool)
    def startConvo(self, handle, initiated):
        self.send_irc.privmsg(handle, f"COLOR >{self.mainwindow.profile().colorcmd()}")
        if initiated:
            self.send_irc.privmsg(handle, "PESTERCHUM:BEGIN")

    @QtCore.pyqtSlot(QString)
    def endConvo(self, handle):
        self.send_irc.privmsg(handle, "PESTERCHUM:CEASE")

    @QtCore.pyqtSlot()
    def updateProfile(self):
        me = self.mainwindow.profile()
        handle = me.handle
        self.send_irc.nick(handle)
        self.mainwindow.closeConversations(True)
        self.mainwindow.doAutoIdentify()
        self.mainwindow.autoJoinDone = False
        self.mainwindow.doAutoJoins()
        self.updateMood()

    @QtCore.pyqtSlot()
    def updateMood(self):
        me = self.mainwindow.profile()
        # Moods via metadata
        self.send_irc.metadata("*", "set", "mood", str(me.mood.value()))
        # Backwards compatibility
        self.send_irc.privmsg("#pesterchum", f"MOOD >{me.mood.value()}")

    @QtCore.pyqtSlot()
    def updateColor(self):
        # Update color metadata field
        color = self.mainwindow.profile().color
        self.send_irc.metadata("*", "set", "color", str(color.name()))
        # Send color messages
        for h in list(self.mainwindow.convos.keys()):
            self.send_irc.privmsg(
                h,
                "COLOR >%s" % (self.mainwindow.profile().colorcmd()),
            )

    @QtCore.pyqtSlot(QString)
    def blockedChum(self, handle):
        self.send_irc.privmsg(handle, "PESTERCHUM:BLOCK")

    @QtCore.pyqtSlot(QString)
    def unblockedChum(self, handle):
        self.send_irc.privmsg(handle, "PESTERCHUM:UNBLOCK")

    @QtCore.pyqtSlot(QString)
    def requestNames(self, channel):
        self.send_irc.names(channel)

    @QtCore.pyqtSlot()
    def requestChannelList(self):
        self.send_irc.list()

    @QtCore.pyqtSlot(QString)
    def joinChannel(self, channel):
        self.send_irc.join(channel)
        self.send_irc.mode(channel)

    @QtCore.pyqtSlot(QString)
    def leftChannel(self, channel):
        self.send_irc.part(channel)

    @QtCore.pyqtSlot(QString, QString, QString)
    def kickUser(self, channel, user, reason=""):
        self.send_irc.kick(channel, user, reason)

    @QtCore.pyqtSlot(QString, QString, QString)
    def setChannelMode(self, channel, mode, command):
        self.send_irc.mode(channel, mode, command)

    @QtCore.pyqtSlot(QString)
    def channelNames(self, channel):
        self.send_irc.names(channel)

    @QtCore.pyqtSlot(QString, QString)
    def inviteChum(self, handle, channel):
        self.send_irc.invite(handle, channel)

    @QtCore.pyqtSlot()
    def pingServer(self):
        self.send_irc.ping("B33")

    @QtCore.pyqtSlot(bool)
    def setAway(self, away=True):
        if away:
            self.away("Idle")
        else:
            self.away()

    @QtCore.pyqtSlot(QString, QString)
    def killSomeQuirks(self, channel, handle):
        self.send_irc.ctcp(channel, "NOQUIRKS", handle)

    @QtCore.pyqtSlot()
    def disconnectIRC(self):
        self.send_irc.quit(f"{_pcVersion} <3")
        self._end = True
        self.close()

    def notice(self, nick, chan, msg):
        """Standard IRC 'NOTICE' message, primarily used for automated replies from services."""
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "NOTICE {} :{}"'.format(handle, msg))
        if (
            handle == "ChanServ"
            and chan == self.mainwindow.profile().handle
            and msg[0:2] == "[#"
        ):
            self.memoReceived.emit(msg[1 : msg.index("]")], handle, msg)
        else:
            self.noticeReceived.emit(handle, msg)

    def metadata(self, target, nick, key, visibility, value):
        """METADATA DRAFT metadata message from server.

        The format of the METADATA server notication is:
        METADATA <Target> <Key> <Visibility> <Value>
        """
        if key.lower() == "mood":
            try:
                mood = Mood(int(value))
                self.moodUpdated.emit(nick, mood)
            except ValueError:
                PchumLog.warning("Invalid mood value, %s, %s", nick, mood)
        elif key.lower() == "color":
            color = QtGui.QColor(value)  # Invalid color becomes rgb 0,0,0
            self.colorUpdated.emit(nick, color)

    def tagmsg(self, prefix, tags, *args):
        """IRCv3 'TAGMSG' message/command, contains a tag without a command.

        For reference see:
        https://ircv3.net/specs/extensions/message-tags.html#the-tagmsg-tag-only-message
        """
        PchumLog.info("TAGMSG: %s %s %s", prefix, tags, args)
        message_tags = tags[1:].split(";")
        for tag in message_tags:
            if tag.startswith("+pesterchum"):
                # Pesterchum tag
                try:
                    key, value = tag.split("=")
                except ValueError:
                    return
                PchumLog.info("Pesterchum tag: %s=%s", key, value)
                # PESTERCHUM: syntax check
                if value in ["BEGIN", "BLOCK", "CEASE", "BLOCK", "BLOCKED", "UNBLOCK", "IDLE", "ME"]:
                    # Process like it's a PESTERCHUM: PRIVMSG
                    msg = "PESTERCHUM:" + value
                    self.privmsg(prefix, args[0], msg)
                elif value.startswith("COLOR>"):
                    # Process like it's a COLOR >0,0,0 PRIVMSG
                    msg = value.replace(">", " >")
                    self.privmsg(prefix, args[0], msg)
                elif value.startswith("TIME>"):
                    # Process like it's a PESTERCHUM:TIME> PRIVMSG
                    msg = "PESTERCHUM:" + value
                    self.privmsg(prefix, args[0], msg)
                else:
                    # Invalid syntax
                    PchumLog.warning("TAGMSG with invalid syntax.")

    def ping(self, _prefix, token):
        """'PING' command from server, we respond with PONG and a matching token."""
        self.send_irc.pong(token)

    def error(self, *params):
        """'ERROR' message from server, the server is terminating our connection."""
        self.stopIRC = " ".join(params).strip()
        self.disconnectIRC()

    def privmsg(self, nick, chan, msg):
        """'PRIVMSG' message from server, the standard message."""
        handle = nick[0 : nick.find("!")]
        if not msg:  # Length 0
            return
        # CTCP
        # ACTION, IRC /me (The CTCP kind)
        if msg[0:8] == "\x01ACTION ":
            msg = "/me" + msg[7:-1]
        # CTCPs that don't need to be shown
        elif msg[0] == "\x01":
            PchumLog.info('---> recv "CTCP {} :{}"'.format(handle, msg[1:-1]))
            # VERSION, return version
            if msg[1:-1].startswith("VERSION"):
                self.send_irc.ctcp(handle, "VERSION", "Pesterchum %s" % (_pcVersion))
            # CLIENTINFO, return supported CTCP commands.
            elif msg[1:-1].startswith("CLIENTINFO"):
                self.send_irc.ctcp(
                    handle,
                    "CLIENTINFO",
                    "ACTION VERSION CLIENTINFO PING SOURCE NOQUIRKS GETMOOD",
                )
            # PING, return pong
            elif msg[1:-1].startswith("PING"):
                if len(msg[1:-1].split("PING ")) > 1:
                    self.send_irc.ctcp(handle, "PING", msg[1:-1].split("PING ")[1])
                else:
                    self.send_irc.ctcp(handle, "PING")
            # SOURCE, return source
            elif msg[1:-1].startswith("SOURCE"):
                self.send_irc.ctcp(
                    handle,
                    "SOURCE",
                    "https://github.com/Dpeta/pesterchum-alt-servers",
                )
            # ???
            elif msg[1:-1].startswith("NOQUIRKS") and chan[0] == "#":
                op = nick[0 : nick.find("!")]
                self.quirkDisable.emit(chan, msg[10:-1], op)
            # GETMOOD via CTCP
            elif msg[1:-1].startswith("GETMOOD"):
                # GETMOOD via CTCP
                # Maybe we can do moods like this in the future...
                mymood = self.mainwindow.profile().mood.value()
                self.send_irc.ctcp(handle, "MOOD >%d" % (mymood))
                # Backwards compatibility
                self.send_irc.privmsg("#pesterchum", "MOOD >%d" % (mymood))
            return

        if chan != "#pesterchum":
            # We don't need anywhere near that much spam.
            PchumLog.info('---> recv "PRIVMSG {} :{}"'.format(handle, msg))

        if chan == "#pesterchum":
            # follow instructions
            if msg[0:6] == "MOOD >":
                try:
                    mood = Mood(int(msg[6:]))
                except ValueError:
                    mood = Mood(0)
                self.moodUpdated.emit(handle, mood)
            elif msg[0:7] == "GETMOOD":
                mychumhandle = self.mainwindow.profile().handle
                mymood = self.mainwindow.profile().mood.value()
                if msg.find(mychumhandle, 8) != -1:
                    self.send_irc.privmsg("#pesterchum", "MOOD >%d" % (mymood))
        elif chan[0] == "#":
            if msg[0:16] == "PESTERCHUM:TIME>":
                self.timeCommand.emit(chan, handle, msg[16:])
            else:
                self.memoReceived.emit(chan, handle, msg)
        else:
            # private message
            # silently ignore messages to yourself.
            if handle == self.mainwindow.profile().handle:
                return
            if msg[0:7] == "COLOR >":
                colors = msg[7:].split(",")
                try:
                    colors = [int(d) for d in colors]
                except ValueError as e:
                    PchumLog.warning(e)
                    colors = [0, 0, 0]
                PchumLog.debug("colors: " + str(colors))
                color = QtGui.QColor(*colors)
                self.colorUpdated.emit(handle, color)
            else:
                self.messageReceived.emit(handle, msg)

    def quit(self, nick, reason):
        """QUIT message from server, a client has quit the server."""
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "QUIT %s: %s"', handle, reason)
        if handle == self.mainwindow.randhandler.randNick:
            self.mainwindow.randhandler.setRunning(False)
        server = self.mainwindow.config.server()
        baseserver = server[server.rfind(".", 0, server.rfind(".")) :]
        if reason.count(baseserver) == 2:
            self.userPresentUpdate.emit(handle, "", "netsplit")
        else:
            self.userPresentUpdate.emit(handle, "", "quit")
        self.moodUpdated.emit(handle, Mood("offline"))

    def kick(self, opnick, channel, handle, reason):
        """'KICK' message from server, someone got kicked from a channel."""
        op = opnick[0 : opnick.find("!")]
        self.userPresentUpdate.emit(handle, channel, f"kick:{op}:{reason}")
        # ok i shouldnt be overloading that but am lazy

    def part(self, nick, channel, _reason="nanchos"):
        """'PART' message from server, someone left a channel."""
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "PART {}: {}"'.format(handle, channel))
        self.userPresentUpdate.emit(handle, channel, "left")
        if channel == "#pesterchum":
            self.moodUpdated.emit(handle, Mood("offline"))

    def join(self, nick, channel):
        """'JOIN' message from server, someone joined a channel."""
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "JOIN {}: {}"'.format(handle, channel))
        self.userPresentUpdate.emit(handle, channel, "join")
        if channel == "#pesterchum":
            if handle == self.mainwindow.randhandler.randNick:
                self.mainwindow.randhandler.setRunning(True)
            self.moodUpdated.emit(handle, Mood("chummy"))

    def mode(self, op, channel, mode, *handles):
        """'MODE' message from server, a user or a channel's mode changed."""
        PchumLog.debug(
            "mode(op=%s, channel=%s, mode=%s, handles=%s)", op, channel, mode, handles
        )
        if not handles:
            handles = [""]
        # opnick = op[0 : op.find("!")]
        # Channel section
        # This might be clunky with non-unrealircd IRC servers
        channel_mode = ""
        unrealircd_channel_modes = "cCdfGHikKLlmMNnOPpQRrsSTtVzZ"
        if any(md in mode for md in unrealircd_channel_modes):
            PchumLog.debug("Channel mode in string.")
            modes = list(self.mainwindow.modes)
            for md in unrealircd_channel_modes:
                if mode.find(md) != -1:  # -1 means not found
                    if mode[0] == "+":
                        modes.extend(md)
                        channel_mode = "+" + md
                    elif mode[0] == "-":
                        try:
                            modes.remove(md)
                            channel_mode = "-" + md
                        except ValueError:
                            PchumLog.warning(
                                "Can't remove channel mode that isn't set."
                            )
                    self.userPresentUpdate.emit(
                        "", channel, f"{channel_mode}:{op}"
                    )
                    PchumLog.debug("pre-mode=%s", mode)
                    mode = mode.replace(md, "")
                    PchumLog.debug("post-mode=%s", mode)
            modes.sort()
            self.mainwindow.modes = "+" + "".join(modes)

        modes = []
        cur = "+"
        for l in mode:
            if l in ["+", "-"]:
                cur = l
            else:
                modes.append(f"{cur}{l}")
        for i, m in enumerate(modes):
            # Server-set usermodes don't need to be passed.
            if handles == [""] and not ("x" in m or "z" in m or "o" in m or "x" in m):
                try:
                    self.userPresentUpdate.emit(handles[i], channel, m + f":{op}")
                except IndexError as e:
                    PchumLog.exception("modeSetIndexError: %s", e)

    def invite(self, sender, _you, channel):
        """'INVITE' message from server, someone invited us to a channel.

        Pizza party everyone invited!!!"""
        handle = sender.split("!")[0]
        self.inviteReceived.emit(handle, channel)

    def nick(self, oldnick, newnick, _hopcount=0):
        """'NICK' message from server, signifies a nick change.

        Is send when our or someone else's nick got changed willingly or unwillingly."""
        PchumLog.info(oldnick, newnick)
        # svsnick
        if oldnick == self.mainwindow.profile().handle:
            # Server changed our handle, svsnick?
            self.getSvsnickedOn.emit(oldnick, newnick)

        # etc.
        oldhandle = oldnick[0 : oldnick.find("!")]
        if self.mainwindow.profile().handle in [newnick, oldhandle]:
            self.myHandleChanged.emit(newnick)
        newchum = PesterProfile(newnick, chumdb=self.mainwindow.chumdb)
        self.moodUpdated.emit(oldhandle, Mood("offline"))
        self.userPresentUpdate.emit(f"{oldhandle}:{newnick}", "", "nick")
        if newnick in self.mainwindow.chumList.chums:
            self.getMood(newchum)
        if oldhandle == self.mainwindow.randhandler.randNick:
            self.mainwindow.randhandler.setRunning(False)
        elif newnick == self.mainwindow.randhandler.randNick:
            self.mainwindow.randhandler.setRunning(True)

    def welcome(self, server, nick, msg):
        """Numeric reply 001 RPL_WELCOME, send when we've connected to the server."""
        self.setConnected()
        # mychumhandle = self.mainwindow.profile().handle
        mymood = self.mainwindow.profile().mood.value()
        color = self.mainwindow.profile().color
        if not self.mainwindow.config.lowBandwidth():
            # Negotiate capabilities
            self.send_irc.cap("REQ", "message-tags")
            self.send_irc.cap(
                self, "REQ", "draft/metadata-notify-2"
            )  # <--- Not required in the unreal5 module implementation
            self.send_irc.cap(
                self, "REQ", "pesterchum-tag"
            )  # <--- Currently not using this
            #time.sleep(0.413 + 0.097)  # <--- somehow, this actually helps.
            self.send_irc.join("#pesterchum")
            # Moods via metadata
            self.send_irc.metadata("*", "sub", "mood")
            self.send_irc.metadata("*", "set", "mood", str(mymood))
            # Color via metadata
            self.send_irc.metadata("*", "sub", "color")
            self.send_irc.metadata("*", "set", "color", str(color.name()))
            # Backwards compatible moods
            self.send_irc.privmsg("#pesterchum", "MOOD >%d" % (mymood))

    def featurelist(self, _target, _handle, *params):
        """Numerical reply 005 RPL_ISUPPORT to communicate supported server features.

        Not in the original specification.
        Metadata support could be confirmed via CAP ACK/CAP NEK.
        """ 
        features = params[:-1]
        PchumLog.info("Server featurelist: %s", features)
        for x in features:
            if x.upper().startswith("METADATA"):
                PchumLog.info("Server supports metadata.")
                self.metadata_supported = True

    def cap(self, server, nick, subcommand, tag):
        """IRCv3 capabilities command from server.

        See: https://ircv3.net/specs/extensions/capability-negotiation
        """
        PchumLog.info("CAP %s %s %s %s", server, nick, subcommand, tag)
        # if tag == "message-tags":
        #    if subcommand == "ACK":

    def umodeis(self, _server, _handle, modes):
        """Numeric reply 221 RPL_UMODEIS, shows us our user modes."""
        self.mainwindow.modes = modes

    def liststart(self, _server, _handle, *info):
        """Numeric reply 321 RPL_LISTSTART, start of list of channels."""
        self.channel_list = []
        info = list(info)
        self.channel_field = info.index("Channel")  # dunno if this is protocol
        PchumLog.info('---> recv "CHANNELS: %s ', self.channel_field)

    def list(self, _server, _handle, *info):
        """Numeric reply 322 RPL_LIST, returns part of the list of channels."""
        channel = info[self.channel_field]
        usercount = info[1]
        if channel not in self.channel_list and channel != "#pesterchum":
            self.channel_list.append((channel, usercount))
        PchumLog.info('---> recv "CHANNELS: %s ', channel)

    def listend(self, _server, _handle, _msg):
        """Numeric reply 323 RPL_LISTEND, end of a series of LIST replies."""
        PchumLog.info('---> recv "CHANNELS END"')
        self.channelListReceived.emit(PesterList(self.channel_list))
        self.channel_list = []

    def channelmodeis(self, _server, _handle, channel, modes, _mode_params=""):
        """Numeric reply 324 RPL_CHANNELMODEIS, gives channel modes."""
        self.modesUpdated.emit(channel, modes)

    def namreply(self, _server, _nick, _op, channel, names):
        """Numeric reply 353 RPL_NAMREPLY, part of a NAMES list of members, usually of a channel."""
        namelist = names.split(" ")
        PchumLog.info('---> recv "NAMES %s: %s names"', channel, len(namelist))
        if not hasattr(self, "channelnames"):
            self.channelnames = {}
        if channel not in self.channelnames:
            self.channelnames[channel] = []
        self.channelnames[channel].extend(namelist)

    def endofnames(self, _server, _nick, channel, _msg):
        """Numeric reply 366 RPL_ENDOFNAMES, end of NAMES list of members, usually of a channel."""
        try:
            namelist = self.channelnames[channel]
        except KeyError:
            # EON seems to return with wrong capitalization sometimes?
            for channel_name in self.channelnames:
                if channel.casefold() == channel_name.casefold():
                    channel = channel_name
                    namelist = self.channelnames[channel]
        del self.channelnames[channel]
        self.namesReceived.emit(channel, PesterList(namelist))
        if channel == "#pesterchum" and not self.joined:
            self.joined = True
            self.mainwindow.randhandler.setRunning(
                self.mainwindow.randhandler.randNick in namelist
            )
            chums = self.mainwindow.chumList.chums
            lesschums = []
            for chum in chums:
                if chum.handle in namelist:
                    lesschums.append(chum)
            self.getMood(*lesschums)

    def cannotsendtochan(self, _server, _handle, channel, msg):
        """Numeric reply 404 ERR_CANNOTSENDTOCHAN, we aren't in the channel or don't have voice."""
        self.cannotSendToChan.emit(channel, msg)

    def erroneusnickname(self, *args):
        """Numeric reply 432 ERR_ERRONEUSNICKNAME, we have a forbidden or protocol-breaking nick."""
        # Server is not allowing us to connect.
        reason = "Handle is not allowed on this server.\n" + " ".join(args)
        self.stopIRC = reason.strip()
        self.disconnectIRC()

    def nicknameinuse(self, _server, _cmd, nick, _msg):
        """Numerical reply 433 ERR_NICKNAMEINUSE, raised when changing nick to nick in use."""
        newnick = "pesterClient%d" % (random.randint(100, 999))
        self.send_irc.nick(newnick)
        self.nickCollision.emit(nick, newnick)

    def nickcollision(self, _server, _cmd, nick, _msg):
        """Numerical reply 436 ERR_NICKCOLLISION, raised during connect when nick is in use."""
        newnick = "pesterClient%d" % (random.randint(100, 999))
        self.send_irc.nick(newnick)
        self.nickCollision.emit(nick, newnick)

    def forbiddenchannel(self, _server, handle, channel, msg):
        """Numeric reply 448 'forbiddenchannel' reply, channel is forbidden.

        Not in the specification but used by UnrealIRCd."""
        self.signal_forbiddenchannel.emit(channel, msg)
        self.userPresentUpdate.emit(handle, channel, "left")

    def inviteonlychan(self, _server, _handle, channel, _msg):
        """Numeric reply 473 ERR_INVITEONLYCHAN, can't join channel (+i)."""
        self.chanInviteOnly.emit(channel)

    def keyvalue(self, target, handle_us, handle_owner, key, visibility, *value):
        """METADATA DRAFT numeric reply 761 RPL_KEYVALUE, we received the value of a key.

        The format of the METADATA server notication is:
        METADATA <Target> <Key> <Visibility> <Value>
        """
        if key == "mood":
            mood = Mood(int(value[0]))
            self.moodUpdated.emit(handle_owner, mood)

    def nomatchingkey(self, _target, _our_handle, failed_handle, _key, *_error):
        """METADATA DRAFT numeric reply 766 ERR_NOMATCHINGKEY, no matching key."""
        PchumLog.info("nomatchingkey: %s", failed_handle)
        # No point in GETMOOD-ing services
        # Fallback to the normal GETMOOD method if getting mood via metadata fails.
        if failed_handle.casefold() not in SERVICES:
            self.send_irc.privmsg("#pesterchum", f"GETMOOD {failed_handle}")

    def keynotset(self, _target, _our_handle, failed_handle, _key, *_error):
        """METADATA DRAFT numeric reply 768 ERR_KEYNOTSET, key isn't set."""
        PchumLog.info("nomatchingkey: %s", failed_handle)
        # Fallback to the normal GETMOOD method if getting mood via metadata fails.
        if failed_handle.casefold() not in SERVICES:
            self.send_irc.privmsg("#pesterchum", f"GETMOOD {failed_handle}")

    def keynopermission(self, _target, _our_handle, failed_handle, _key, *_error):
        """METADATA DRAFT numeric reply 769 ERR_KEYNOPERMISSION, no permission for key."""
        PchumLog.info("nomatchingkey: %s", failed_handle)
        # Fallback to the normal GETMOOD method if getting mood via metadata fails.
        if failed_handle.casefold() not in SERVICES:
            self.send_irc.privmsg("#pesterchum", f"GETMOOD {failed_handle}")

    def metadatasubok(self, *params):
        """"METADATA DRAFT numeric reply 770 RPL_METADATASUBOK, we subbed to a key."""
        PchumLog.info("metadatasubok: %s", params)

    def run_command(self, command, *args):
        """Finds and runs a command."""
        PchumLog.debug("run_command %s(%s)", command, args)
        try:
            command_function = self.commands[command]
        except KeyError:
            PchumLog.warning("No matching function for command: %s(%s)", command, args)
            return
        
        try:
            command_function(*args)
        except TypeError:
            PchumLog.exception("Failed to pass command, did the server pass an unsupported paramater?")
        except Exception:
            PchumLog.exception("Exception while parsing command.")

    moodUpdated = QtCore.pyqtSignal("QString", Mood)
    colorUpdated = QtCore.pyqtSignal("QString", QtGui.QColor)
    messageReceived = QtCore.pyqtSignal("QString", "QString")
    memoReceived = QtCore.pyqtSignal("QString", "QString", "QString")
    noticeReceived = QtCore.pyqtSignal("QString", "QString")
    inviteReceived = QtCore.pyqtSignal("QString", "QString")
    timeCommand = QtCore.pyqtSignal("QString", "QString", "QString")
    namesReceived = QtCore.pyqtSignal("QString", PesterList)
    channelListReceived = QtCore.pyqtSignal(PesterList)
    nickCollision = QtCore.pyqtSignal("QString", "QString")
    getSvsnickedOn = QtCore.pyqtSignal("QString", "QString")
    myHandleChanged = QtCore.pyqtSignal("QString")
    chanInviteOnly = QtCore.pyqtSignal("QString")
    modesUpdated = QtCore.pyqtSignal("QString", "QString")
    connected = QtCore.pyqtSignal()
    askToConnect = QtCore.pyqtSignal(Exception)
    userPresentUpdate = QtCore.pyqtSignal("QString", "QString", "QString")
    cannotSendToChan = QtCore.pyqtSignal("QString", "QString")
    quirkDisable = QtCore.pyqtSignal("QString", "QString", "QString")
    signal_forbiddenchannel = QtCore.pyqtSignal("QString", "QString")

class SendIRC:
    """Provides functions for outgoing IRC commands."""
    def __init__(self):
        self.socket = None  # INET socket connected with server.

    def send(self, *args: str, text=None):
        """Send a command to the IRC server.

        Takes either a string or a list of strings.
        The 'text' argument is for the final parameter, which can have spaces."""
        # Return if disconnected
        if not self.socket or self.socket.fileno() == -1:
            PchumLog.error(
                "Send attempted while disconnected, args: %s, text: %s.", args, text
            )
            return

        command = ""
        # Convert command arguments to a single string if passed.
        if args:
            command += " ".join(args)
        # If text is passed, add ':' to imply everything after it is one parameter.
        if text:
            command += f" :{text}"
        # Add characters for end of line in IRC.
        command += "\r\n"
        # UTF-8 is the prefered encoding in 2023.
        outgoing_bytes = command.encode(encoding="utf-8", errors="replace")

        try:
            PchumLog.debug("Sending: %s", command)
            self.socket.send(outgoing_bytes)
        except OSError:
            PchumLog.exception("Error while sending: '%s'", command.strip())
            self.socket.close()

    def ping(self, token):
        """Send PING command to server to check for connectivity."""
        self.send("PING", text=token)

    def pong(self, token):
        """Send PONG command to reply to server PING."""
        self.send("PONG", token)

    def nick(self, nick):
        """Send USER command to communicate nick to server."""
        self.send("NICK", nick)

    def user(self, username, realname):
        """Send USER command to communicate username and realname to server."""
        self.send("USER", username, "0", "*", text=realname)

    def privmsg(self, target, text):
        """Send PRIVMSG command to send a message."""
        for line in text.split("\n"):
            self.send("PRIVMSG", target, text=line)

    def names(self, channel):
        """Send NAMES command to view channel members."""
        self.send("NAMES", channel)

    def kick(self, channel, user, reason=""):
        """Send KICK command to force user from channel."""
        if reason:
            self.send(f"KICK {channel} {user}", text=reason)
        else:
            self.send(f"KICK {channel} {user}")

    def mode(self, target, modestring="", mode_arguments=""):
        """Set or remove modes from target."""
        outgoing_mode = " ".join([target, modestring, mode_arguments]).strip()
        self.send("MODE", outgoing_mode)

    def ctcp(self, target, command, msg=""):
        """Send Client-to-Client Protocol message."""
        outgoing_ctcp = " ".join(
            [command, msg]
        ).strip()  # Extra spaces break protocol, so strip.
        self.privmsg(target, f"\x01{outgoing_ctcp}\x01")

    def metadata(self, target, subcommand, *params):
        """Send Metadata command to get or set metadata.

        See IRC metadata draft specification:
        https://gist.github.com/k4bek4be/92c2937cefd49990fbebd001faf2b237
        """
        self.send("METADATA", target, subcommand, *params)

    def cap(self, subcommand, *params):
        """Send IRCv3 CAP command for capability negotiation.

        See: https://ircv3.net/specs/extensions/capability-negotiation.html"""
        self.send("CAP", subcommand, *params)

    def join(self, channel, key=""):
        """Send JOIN command to join a channel/memo.

        Keys or joining multiple channels is possible in the specification, but unused.
        """
        channel_and_key = " ".join([channel, key]).strip()
        self.send("JOIN", channel_and_key)

    def part(self, channel):
        """Send PART command to leave a channel/memo.

        Providing a reason or leaving multiple channels is possible in the specification.
        """
        self.send("PART", channel)

    def notice(self, target, text):
        """Send a NOTICE to a user or channel."""
        self.send("NOTICE", target, text=text)

    def invite(self, nick, channel):
        """Send INVITE command to invite a user to a channel."""
        self.send("INVITE", nick, channel)

    def away(self, text=None):
        """AWAY command to mark client as away or no longer away.

        No 'text' parameter means the client is no longer away."""
        if text:
            self.send("AWAY", text=text)
        else:
            self.send("AWAY")

    def list(self):
        """Send LIST command to get list of channels."""
        self.send("LIST")

    def quit(self, reason=""):
        """Send QUIT to terminate connection."""
        self.send("QUIT", text=reason)
