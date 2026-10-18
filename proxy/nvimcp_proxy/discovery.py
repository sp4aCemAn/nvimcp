import glob
import json
import os
import subprocess
import sys
import tempfile

from .errors import ProxyError


class SessionFinder:
    """Session selection policy: registry files, terminal ancestry, cwd ranking.

    Attributes keep the proxy's arg names so the moved method bodies work
    unchanged. log/warn are the proxy's debug/warning sinks."""

    def __init__(self, socket_arg, pid_arg, sessions_file_arg, skip_ancestry, log, warn):
        self.socket_arg = socket_arg
        self.pid_arg = pid_arg
        self.sessions_file_arg = sessions_file_arg
        self.skip_ancestry = skip_ancestry
        self._log = log
        self._warn = warn
        self._ancestors = None

    def _ancestor_pids(self):
        """Ancestor chain, computed once and cached.

        Discovery re-runs on every retry while parked; re-walking with ps(1)
        each time is a process storm under load (32 spawns per pass), and a
        timed-out walk truncates the chain — losing the exact-owner match and
        silently falling back to cwd heuristics. The chain is launch-time
        truth; a re-walk after an intermediate shell exits follows the
        re-parented chain and misses the nvim ancestor entirely."""
        if self._ancestors is None:
            self._ancestors = self._ancestor_pid_chain()
        return self._ancestors

    def _ancestor_pid_chain(self):
        """Nearest-first pids of this process's ancestors.

        An agent (and therefore this proxy) launched inside an nvim terminal
        window is a descendant of that nvim session's process, so matching a
        session pid in this chain is an exact owner match — even when the
        agent's shell cd'ed elsewhere. ps(1) keeps this portable on both
        Linux and macOS (no /proc needed)."""
        chain = []
        pid = os.getpid()
        for _ in range(32):
            try:
                out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
            except Exception:
                break
            try:
                ppid = int(out.strip().split()[0]) if out.strip() else 0
            except (ValueError, IndexError):
                break
            if ppid <= 1:
                break
            chain.append(ppid)
            pid = ppid
        return chain

    def _session_candidates(self, base):
        """Registry files under one runtime base: legacy sessions.json plus the
        per-pid shards in base/sessions/. Both hold the same {pid: entry} shape,
        so the discovery loop parses them interchangeably."""
        paths = []
        legacy = os.path.join(base, "sessions.json")
        if os.path.isfile(legacy):
            paths.append(legacy)
        paths += sorted(glob.glob(os.path.join(base, "sessions", "*.json")))
        return paths

    def _sessions_files(self):
        if self.sessions_file_arg:
            if os.path.isdir(self.sessions_file_arg):
                cands = self._session_candidates(self.sessions_file_arg)
                return cands or [self.sessions_file_arg]
            return [self.sessions_file_arg]
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return self._session_candidates(os.path.join(xdg, "nvimcp"))
        if sys.platform == "darwin":
            # stdpath("run") on macOS is $TMPDIR/nvim.<user>/<random> (unique per
            # nvim process), so session registry paths are not stable: find every
            # running session by globbing (the dir may be named after the
            # username or the uid).
            tmp = tempfile.gettempdir()
            paths = []
            for b in sorted(glob.glob(os.path.join(tmp, "nvim.*"))):
                for d in sorted(glob.glob(os.path.join(b, "*", "nvimcp"))):
                    for p in self._session_candidates(d):
                        if p not in paths:
                            paths.append(p)
            return paths
        return self._session_candidates("/run/user/%d/nvimcp" % os.getuid())

    def _describe(self, entries):
        return ", ".join("pid %s (%s)" % (e.get("pid"), e.get("cwd", "?")) for e in entries)

    def _session_alive(self, entry):
        pid = entry.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def _related(self, session_cwd, cli_cwd):
        if not session_cwd or not cli_cwd:
            return False
        s = os.path.realpath(session_cwd)
        c = os.path.realpath(cli_cwd)
        if s == c:
            return True
        common = os.path.commonpath([s, c])
        return common == s or common == c

    def _rank(self, entry, cli_cwd):
        s = entry.get("cwd")
        if not s:
            return 99
        s = os.path.realpath(s)
        c = os.path.realpath(cli_cwd)
        if s == c:
            return 0
        common = os.path.commonpath([s, c])
        if common not in (s, c):
            return 99

        def depth(p):
            return len([x for x in os.path.relpath(p, common).split(os.sep) if x not in (".", "")])

        return depth(s) + depth(c)

    def discover(self):
        if self.socket_arg:
            return {"socket": self.socket_arg, "pid": None}
        paths = self._sessions_files()
        if (
            not paths
            and not self.sessions_file_arg
            and not os.environ.get("XDG_RUNTIME_DIR")
            and sys.platform != "darwin"
        ):
            raise ProxyError(
                "no sessions registry at /run/user/%d/nvimcp (sessions.json or sessions/*.json); "
                "is nvim running with nvimcp started? "
                "(this is the Linux default path; on macOS pass --sessions-file, --pid "
                "or --socket)" % os.getuid()
            )
        entries = []
        seen_sockets = set()
        for path in paths:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                self._warn("cannot read sessions file %s: %s" % (path, e))
                continue
            for e in (data or {}).values():
                if isinstance(e, dict) and isinstance(e.get("socket"), str) and e["socket"] not in seen_sockets:
                    seen_sockets.add(e["socket"])
                    e["_mtime"] = os.path.getmtime(path)
                    entries.append(e)
        alive = [e for e in entries if self._session_alive(e)]
        if len(alive) < len(entries):
            dead = [e for e in entries if e not in alive]
            self._log("ignoring dead sessions: %s" % self._describe(dead))
            entries = alive
        if self.pid_arg is not None:
            for e in entries:
                if e.get("pid") == self.pid_arg:
                    return e
            extra = "; available: %s" % self._describe(entries) if entries else ""
            raise ProxyError("no nvimcp session with pid %s%s" % (self.pid_arg, extra))
        # Terminal ancestry: an agent launched inside an nvim terminal (any
        # child of an nvimcp session's process tree) belongs to that session,
        # regardless of the shell's cwd.
        if not self.skip_ancestry:
            for apid in self._ancestor_pids():
                for e in entries:
                    if e.get("pid") == apid:
                        self._log("matched session pid %s via terminal ancestry" % apid)
                        return e
        cwd = os.getcwd()
        cwd_hits = [e for e in entries if e.get("cwd") == cwd]
        if len(cwd_hits) == 1:
            return cwd_hits[0]
        if len(cwd_hits) > 1:
            self._warn("multiple sessions in this directory; picking pid %s" % cwd_hits[0].get("pid"))
            return cwd_hits[0]
        related = [e for e in entries if self._related(e.get("cwd"), cwd)]
        related.sort(key=lambda e: self._rank(e, cwd))
        if related:
            if len(related) > 1 and self._rank(related[0], cwd) == self._rank(related[1], cwd):
                self._warn(
                    "several sessions related to %s; picking pid %s (pass --pid or --socket to pin)"
                    % (cwd, related[0].get("pid"))
                )
            return related[0]
        if not entries:
            raise ProxyError(
                "no nvimcp sessions found in %s; start nvim and run :NvimcpStart" % (", ".join(paths) or "default locations")
            )
        # No cwd-related session anywhere. Sole live session is unambiguous —
        # connect. Otherwise stay strict: only sessions whose nvimcp config has
        # fallback_recent=true may be picked (their opt-in is published in the
        # sessions file); zero or multiple opted-in sessions -> loud error.
        if len(entries) > 1:
            opted = [e for e in entries if e.get("fallback_recent")]
            if not opted:
                raise ProxyError(
                    "no nvimcp session related to %s; set fallback_recent=true in the nvimcp "
                    "config of the session you want agents opened anywhere to reach, or pass "
                    "--pid/--socket. Available: %s" % (cwd, self._describe(entries))
                )
            if len(opted) > 1:
                raise ProxyError(
                    "no session related to %s and several opted in to fallback_recent; pass "
                    "--pid or --socket. Available (opted): %s" % (cwd, self._describe(opted))
                )
            newest = max(opted, key=lambda e: e.get("registered_at") or e.get("_mtime") or 0)
            self._warn(
                "no nvimcp session related to %s; connecting to opted-in session pid %s (%s)"
                % (cwd, newest.get("pid"), newest.get("cwd"))
            )
            return newest
        return entries[0]
