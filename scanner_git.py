#!/usr/bin/env python3
"""
Shared git-publish helper for all Undercurrent Pi scanners.

WHY THIS EXISTS
Five scanner processes (market, congress, insider, sector-history,
backtest) commit into ONE shared clone of the Stock-Stalker repo. That
shared-clone design is what caused two separate data-feed freezes:

  1. Pushing without pulling first → one outside push made every later
     push non-fast-forward-reject, forever.
  2. Pulling with --rebase while another scanner had left a file
     modified-but-uncommitted → "cannot pull with rebase: unstaged
     changes", every cycle.

Rather than keep patching individual symptoms, this helper makes the
shared clone robust by design:

  * A file lock (flock) SERIALIZES every scanner's git work, so two of
    them can never interleave and corrupt each other's index/rebase.
  * Each cycle first SELF-HEALS any leftover mess — an interrupted
    rebase/merge or a stale index.lock left by a crashed or killed run —
    so a wedged repo un-wedges itself on the very next run instead of
    freezing until a human intervenes.
  * The push itself does pull --rebase --autostash (reconcile with
    outside pushes; tolerate a dirty working tree), and on any failure
    it simply skips this cycle and retries next time.

Net effect: a crash, a race, an outside push, or a stray dirty file can
delay one publish cycle, but none of them can permanently freeze the
feed. Import and call commit_and_push().
"""

import os
import time
import fcntl
import shutil
import logging
import subprocess

log = logging.getLogger("scanner_git")

GIT_TIMEOUT = 90     # seconds per individual git command (hung SSH, etc.)
LOCK_TIMEOUT = 150   # max seconds to wait for another scanner's git op


def _git(repo_dir, args, **kw):
    return subprocess.run(["git", "-C", repo_dir] + args, timeout=GIT_TIMEOUT, **kw)


def _acquire(lock_fd):
    """Block until we hold the exclusive lock, or give up after
    LOCK_TIMEOUT so a hung holder can't stall us forever."""
    deadline = time.time() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            if time.time() > deadline:
                return False
            time.sleep(1)


def _self_heal(repo_dir):
    """Recover from any git state a crashed/killed run may have left
    behind. Safe to call only while holding the lock (guarantees no other
    scanner is mid-operation, so a leftover index.lock is genuinely
    stale, not live)."""
    git_dir = os.path.join(repo_dir, ".git")
    # Abort a half-finished rebase. `rebase --abort` handles a valid
    # interrupted rebase; if the state dir is malformed (or abort can't
    # clear it), force-remove it so it can't wedge the next pull --rebase.
    for d in ("rebase-merge", "rebase-apply"):
        p = os.path.join(git_dir, d)
        if os.path.isdir(p):
            log.warning(f"Found an interrupted rebase ({d}) — clearing it before publishing")
            _git(repo_dir, ["rebase", "--abort"], capture_output=True)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
    # Abort a half-finished merge.
    if os.path.exists(os.path.join(git_dir, "MERGE_HEAD")):
        log.warning("Found an interrupted merge — aborting it before publishing")
        _git(repo_dir, ["merge", "--abort"], capture_output=True)
    # Remove a stale index.lock left by a process that died mid-command.
    stale_lock = os.path.join(git_dir, "index.lock")
    if os.path.exists(stale_lock):
        log.warning("Removing a stale index.lock left by a previous run")
        try:
            os.remove(stale_lock)
        except OSError:
            pass


def commit_and_push(repo_dir, files, message):
    """Serialized, self-healing commit + push of `files` with `message`.

    Returns True if it pushed, False if it skipped this cycle (nothing to
    commit, lock contention, or a transient failure — all safe to retry
    next cycle). Never raises: a publish problem must never take down the
    scan loop that calls it.
    """
    if not repo_dir:
        log.warning("REPO_DIR not set — nothing to commit/push")
        return False

    lock_path = os.path.join(repo_dir, ".git", "scanner-publish.lock")
    try:
        lock_fd = open(lock_path, "w")
    except OSError as e:
        log.error(f"Couldn't open publish lock ({e}); skipping push this cycle")
        return False

    try:
        if not _acquire(lock_fd):
            log.warning("Another scanner held the git lock past the timeout; "
                        "skipping push this cycle (will retry next time)")
            return False
        try:
            _self_heal(repo_dir)
            _git(repo_dir, ["add"] + list(files), check=True)
            if _git(repo_dir, ["diff", "--cached", "--quiet"]).returncode == 0:
                log.info("No changes since last push — skipping commit")
                return False
            _git(repo_dir, ["commit", "-m", message], check=True)
            pull = _git(repo_dir, ["pull", "--rebase", "--autostash", "origin", "main"],
                        capture_output=True)
            if pull.returncode != 0:
                _git(repo_dir, ["rebase", "--abort"], capture_output=True)
                err = pull.stderr.decode(errors="replace")[:200] if pull.stderr else ""
                log.error(f"git pull --rebase failed; skipping push this cycle (will retry): {err}")
                return False
            _git(repo_dir, ["push"], check=True)
            log.info("Pushed to GitHub — Vercel will redeploy shortly")
            return True
        except subprocess.CalledProcessError as e:
            log.error(f"git command failed; skipping push this cycle (will retry): {e}")
            return False
        except subprocess.TimeoutExpired as e:
            log.error(f"git command timed out (hung connection?); skipping push this cycle: {e}")
            return False
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        lock_fd.close()
