"""Wake a Chromecast-built-in TV by launching a Cast receiver on it.

Google TVs keep their Cast receiver listening in network standby, and being cast to is one of the few
things that turns them on, so this can work when Wake-on-LAN and ADB cannot. Whether it does depends on
the TV; the log records what happened. Needs `pip install pychromecast`.
"""
import uuid

from . import DeviceError

DEFAULT_MEDIA_RECEIVER = "CC1AD845"


def cast_wake(host, port=8009, timeout=8):
    try:
        import pychromecast
    except ImportError:
        raise DeviceError("pychromecast is not installed (pip install pychromecast)")
    cast = None
    try:
        cast = pychromecast.get_chromecast_from_host((host, port, uuid.uuid4(), None, None), tries=1, timeout=timeout)
        cast.wait(timeout=timeout)
        cast.start_app(DEFAULT_MEDIA_RECEIVER, timeout=timeout)
    except Exception as e:  # pychromecast raises several unrelated error types
        raise DeviceError(f"Cast launch failed ({type(e).__name__}: {e})")
    finally:
        if cast is not None:
            try:
                cast.disconnect(timeout=2)
            except Exception:
                pass
    return f"launched the Cast receiver on {host}:{port}"
