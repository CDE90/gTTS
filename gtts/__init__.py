# -*- coding: utf-8 -*-
from .tts import gTTS, gTTSError
from .tts_async import gTTSAsync
from .version import __version__  # noqa: F401

__all__ = ["__version__", "gTTS", "gTTSError", "gTTSAsync"]
