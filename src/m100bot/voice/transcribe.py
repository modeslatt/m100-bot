"""Голосовое сообщение (OGG/Opus из Telegram) → текст.

По умолчанию локально через faster-whisper (pip install -e ".[voice]");
STT_PROVIDER=api — через OpenAI-совместимый Whisper API.
"""


class Transcriber:
    def __init__(self, provider: str, model: str, device: str = "cpu", **api_kwargs: str) -> None:
        self._provider = provider
        self._model_name = model
        self._device = device
        self._api_kwargs = api_kwargs

    async def transcribe(self, audio: bytes) -> str:
        """TODO:
        local: WhisperModel(model, device, compute_type="int8") — загрузить один раз при старте;
               transcribe в asyncio.to_thread, language="ru", vad_filter=True,
               initial_prompt="медь, фреонопровод, изоляция, кабель 3х2,5, хомут, шпилька, гофра".
               OGG читается напрямую (faster-whisper использует PyAV, ffmpeg ставить не нужно).
        api:   AsyncOpenAI(...).audio.transcriptions.create(model, file=("voice.ogg", audio)).
        """
        raise NotImplementedError
