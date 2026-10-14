import re

from django.conf import settings

from ..exceptions import AgentUnavailable


def chunk_section(text, limit=900):
    """Gộp các đoạn văn thành chunk <= limit ký tự (không cắt giữa đoạn)."""
    parts, current = [], ''
    for para in re.split(r'\n{2,}', text.strip()):
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) + 2 > limit:
            parts.append(current)
            current = ''
        current = f'{current}\n\n{para}' if current else para
    if current:
        parts.append(current)
    return parts


def embed_texts(texts, task_type):
    """task_type: RETRIEVAL_DOCUMENT (lưu) hoặc RETRIEVAL_QUERY (câu hỏi)."""
    if not settings.CHATBOT_GOOGLE_API_KEY:
        raise AgentUnavailable('Thiếu GOOGLE_API_KEY.')
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.CHATBOT_GOOGLE_API_KEY,
                          http_options=types.HttpOptions(timeout=20000))
    vectors = []
    for i in range(0, len(texts), 50):
        resp = client.models.embed_content(
            model=settings.CHATBOT_EMBED_MODEL, contents=texts[i:i + 50],
            config=types.EmbedContentConfig(task_type=task_type,
                                            output_dimensionality=settings.CHATBOT_EMBED_DIM))
        vectors.extend(e.values for e in resp.embeddings)
    return vectors