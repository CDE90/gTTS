import base64
import json
import logging
import os
import re
import tempfile
import urllib

import aiofiles
import aiohttp

from gtts.lang import _fallback_deprecated_lang, tts_langs
from gtts.tokenizer import Tokenizer, pre_processors, tokenizer_cases
from gtts.utils import _clean_tokens, _minimize, _translate_url

__all__ = ["gTTSAsync", "gTTSError"]

# Logger
log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class Speed:
    SLOW = True
    NORMAL = None


class gTTSAsync:
    GOOGLE_TTS_MAX_CHARS = 100
    GOOGLE_TTS_HEADERS = {
        "Referer": "http://translate.google.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/47.0.2526.106 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    GOOGLE_TTS_RPC = "jQ1olc"

    def __init__(
        self,
        text,
        tld="com",
        lang="en",
        slow=False,
        lang_check=True,
        pre_processor_funcs=[
            pre_processors.tone_marks,
            pre_processors.end_of_line,
            pre_processors.abbreviations,
            pre_processors.word_sub,
        ],
        tokenizer_func=Tokenizer(
            [
                tokenizer_cases.tone_marks,
                tokenizer_cases.period_comma,
                tokenizer_cases.colon,
                tokenizer_cases.other_punctuation,
            ]
        ).run,
        timeout=None,
    ):
        # Debug
        for k, v in dict(locals()).items():
            if k == "self":
                continue
            log.debug("%s: %s", k, v)

        # Text
        assert text, "No text to speak"
        self.text = text

        # Translate URL top-level domain
        self.tld = tld

        # Language
        self.lang_check = lang_check
        self.lang = lang

        if self.lang_check:
            # Fallback lang in case it is deprecated
            self.lang = _fallback_deprecated_lang(lang)

            try:
                langs = tts_langs()
                if self.lang not in langs:
                    raise ValueError("Language not supported: %s" % lang)
            except RuntimeError as e:
                log.debug(str(e), exc_info=True)
                log.warning(str(e))

        # Read speed
        if slow:
            self.speed = Speed.SLOW
        else:
            self.speed = Speed.NORMAL

        # Pre-processors and tokenizer
        self.pre_processor_funcs = pre_processor_funcs
        self.tokenizer_func = tokenizer_func

        self.timeout = timeout

    def _tokenize(self, text):
        # Pre-clean
        text = text.strip()

        # Apply pre-processors
        for pp in self.pre_processor_funcs:
            log.debug("pre-processing: %s", pp)
            text = pp(text)

        if len(text) <= self.GOOGLE_TTS_MAX_CHARS:
            return _clean_tokens([text])

        # Tokenize
        log.debug("tokenizing: %s", self.tokenizer_func)
        tokens = self.tokenizer_func(text)

        # Clean
        tokens = _clean_tokens(tokens)

        # Minimize
        min_tokens = []
        for t in tokens:
            min_tokens += _minimize(t, " ", self.GOOGLE_TTS_MAX_CHARS)

        # Filter empty tokens, post-minimize
        tokens = [t for t in min_tokens if t]

        return tokens

    def _prepare_rpc_data(self, text):
        parameter = [text, self.lang, self.speed, "null"]
        escaped_parameter = json.dumps(parameter, separators=(",", ":"))

        rpc = [[[self.GOOGLE_TTS_RPC, escaped_parameter, None, "generic"]]]
        escaped_rpc = json.dumps(rpc, separators=(",", ":"))
        return "f.req={}&".format(urllib.parse.quote(escaped_rpc))

    def _prepare_requests(self):
        translate_url = _translate_url(
            tld=self.tld, path="_/TranslateWebserverUi/data/batchexecute"
        )

        text_parts = self._tokenize(self.text)
        log.debug("text_parts: %s", str(text_parts))
        log.debug("text_parts: %i", len(text_parts))
        assert text_parts, "No text to send to TTS API"

        request_params = []
        for idx, part in enumerate(text_parts):
            data = self._prepare_rpc_data(part)
            log.debug("data-%i: %s", idx, data)
            request_params.append(
                {
                    "method": "POST",
                    "url": translate_url,
                    "data": data,
                    "headers": self.GOOGLE_TTS_HEADERS,
                }
            )

        return request_params

    async def stream(self):
        request_params = self._prepare_requests()
        async with aiohttp.ClientSession() as session:
            for idx, params in enumerate(request_params):
                try:
                    async with session.post(
                        url=params["url"],
                        data=params["data"],
                        headers=params["headers"],
                        ssl=False,
                        timeout=self.timeout,
                    ) as r:
                        log.debug("headers-%i: %s", idx, r.request_info.headers)
                        log.debug("url-%i: %s", idx, r.request_info.url)
                        log.debug("status-%i: %s", idx, r.status)

                        r.raise_for_status()

                        content = await r.text()
                        log.debug(
                            "Response content: %s", content[:200]
                        )  # Log first 200 chars of content

                        if not content:
                            raise gTTSError(
                                tts=self,
                                response=r,
                                msg="Empty response received from TTS API",
                            )

                        audio_content = self._extract_audio_content(content)
                        if audio_content:
                            yield audio_content
                        else:
                            raise gTTSError(
                                tts=self,
                                response=r,
                                msg="No audio content found in the response",
                            )

                        log.debug("part-%i created", idx)
                except aiohttp.ClientResponseError as e:
                    log.debug(str(e))
                    raise gTTSError(tts=self, response=r)
                except aiohttp.ClientError as e:
                    log.debug(str(e))
                    raise gTTSError(tts=self)

    def _extract_audio_content(self, content):
        audio_search = re.search(r'jQ1olc","\[\\"(.*?)\\"]', content)
        if audio_search:
            as_bytes = audio_search.group(1).encode("ascii")
            return base64.b64decode(as_bytes)
        return None

    async def write_to_fp(self, fp):
        try:
            async for decoded in self.stream():
                await fp.write(decoded)
                log.debug("part written to %s", fp)
        except (AttributeError, TypeError) as e:
            raise TypeError(
                "'fp' is not a file-like object or it does not take bytes: %s" % str(e)
            )

    async def save(self, savefile):
        async with aiofiles.open(str(savefile), mode="wb") as f:
            await self.write_to_fp(f)
            await f.flush()
            log.debug("Saved to %s", savefile)

    async def get_audio_data(self):
        audio_data = b""
        async for chunk in self.stream():
            audio_data += chunk
        return audio_data

    async def save_to_temp_file(self):
        audio_data = await self.get_audio_data()

        # Create a temporary file
        fd, path = tempfile.mkstemp(suffix=".mp3")
        try:
            with os.fdopen(fd, "wb") as tmp:
                # Write the audio data to the temporary file
                tmp.write(audio_data)
            return path
        except Exception as e:
            os.unlink(path)  # Make sure to remove the file if there's an error
            raise e


class gTTSError(Exception):
    """Exception that uses context to present a meaningful error message"""

    def __init__(self, msg=None, **kwargs):
        self.tts = kwargs.pop("tts", None)
        self.rsp = kwargs.pop("response", None)
        if msg:
            self.msg = msg
        elif self.tts is not None:
            self.msg = self.infer_msg(self.tts, self.rsp)
        else:
            self.msg = None
        super(gTTSError, self).__init__(self.msg)

    def infer_msg(self, tts, rsp=None):
        """Attempt to guess what went wrong by using known
        information (e.g. http response) and observed behaviour
        """
        cause = "Unknown"

        if rsp is None:
            premise = "Failed to connect"

            if tts.tld != "com":
                host = _translate_url(tld=tts.tld)
                cause = "Host '{}' is not reachable".format(host)

        else:
            # rsp should be <aiohttp.ClientResponse>
            status = rsp.status
            reason = rsp.reason

            premise = "{:d} ({}) from TTS API".format(status, reason)

            if status == 403:
                cause = "Bad token or upstream API changes"
            elif status == 404 and tts.tld != "com":
                cause = "Unsupported tld '{}'".format(tts.tld)
            elif status == 200:
                cause = "No audio stream in response. Response content: {}".format(
                    rsp._body[:100]
                )
            elif status >= 500:
                cause = "Upstream API error. Try again later."

        return "{}. Probable cause: {}".format(premise, cause)
