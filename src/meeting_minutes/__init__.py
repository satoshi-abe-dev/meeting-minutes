"""A fully local tool that generates minutes (Markdown) from a meeting recording.

Audio, video, transcription, and summarization all run entirely on the
machine; nothing is sent to an external service. Only the LLM/VLM are
called, over localhost, on a local server (LM Studio, etc.).
"""

__version__ = "0.1.0"
