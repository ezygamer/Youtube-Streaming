"""Stops Windows from sleeping while the streamer runs (24/7 operation).

Uses SetThreadExecutionState, the API media players use. It only lasts while
this process runs, and the screen may still turn off (that's fine: capture
doesn't need a visible window). No-op on other platforms.
"""
import ctypes
import logging
import os

log = logging.getLogger("streamer.power")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040


def keep_awake(enable: bool = True) -> bool:
    if os.name != "nt":
        return False
    flags = _ES_CONTINUOUS
    if enable:
        flags |= _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
    ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    if enable:
        (log.info if ok else log.warning)("Keep-awake %s", "enabled (PC will not sleep)" if ok else "could not be enabled")
    return ok
