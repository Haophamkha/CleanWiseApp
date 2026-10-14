import logging
import re
import unicodedata

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from pgvector.django import CosineDistance

from ..models import HelpArticle, HelpChunk

logger = logging.getLogger(__name__)
RRF_K = 60


def published_articles(role='CUSTOMER'):
    today = timezone.localdate()
    return HelpArticle.objects.filter(status=HelpArticle.Status.PUBLISHED, role=role,
                                      effective_from__lte=today).filter(
        Q(effective_until__isnull=True) | Q(effective_until__gte=today))


def normalize(text):
    text = unicodedata.normalize('NFD', text.lower().replace('đ', 'd'))
    return re.sub(r'[^a-z0-9 ]', ' ', ''.join(c for c in text if not unicodedata.combining(c)))


STOP = set('toi cua la va thi co khong nhu nao lam sao cho voi mot cac duoc ban hay gi ve nay minh xin muon can the'.split())
ALIASES = {'vietqr': 'chuyen khoan qr', 'voucher': 'ma giam gia', 'topup': 'nap tien',
           'booking': 'dat don', 'cash': 'tien mat', 'wallet': 'vi', 'refund': 'hoan tien'}


def keyword_hits(query, limit=8, role='CUSTOMER'):
    """Tìm theo từ khóa (không dấu). Trả [(article, section_index, section)] đã xếp hạng."""
    query = normalize(query)
    for term, expansion in ALIASES.items():
        if term in query.split():
            query += ' ' + expansion
    tokens = set(query.split()) - STOP
    if not tokens:
        return []
    ranked = []
    for article in published_articles(role):
        for index, section in enumerate(article.sections):
            heading = normalize(section['heading'])
            body = normalize(section['text'])
            matched = tokens & set((heading + ' ' + body).split())
            coverage = len(matched) / len(tokens)
            if not matched or coverage < 0.45 or (len(tokens) > 1 and len(matched) < 2):
                continue
            score = coverage * 10 + len(tokens & set(heading.split())) * 2
            ranked.append((score, article.pk, index, article, section))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [(a, i, s) for _, _, i, a, s in ranked[:limit]]


def vector_hits(query, limit=8, query_vector=None, role='CUSTOMER'):
    """Truy hồi theo embedding. Truyền query_vector nếu đã embed sẵn để khỏi gọi API lại."""
    from .embedding import embed_texts
    if not settings.CHATBOT_GOOGLE_API_KEY or not HelpChunk.objects.exists():
        return []
    vec = query_vector if query_vector is not None else embed_texts([query], 'RETRIEVAL_QUERY')[0]
    return list(
        HelpChunk.objects.filter(article__in=published_articles(role).values('pk'))
        .annotate(distance=CosineDistance('embedding', vec))
        .filter(distance__lte=settings.CHATBOT_RAG_MAX_DISTANCE)
        .order_by('distance').select_related('article')[:limit]
    )


def search_help(query, limit=3, query_vector=None, role='CUSTOMER'):
    keyword = keyword_hits(query, role=role)
    try:
        vector = vector_hits(query, query_vector=query_vector, role=role)
    except Exception as exc:  # embedding lỗi thì vẫn trả lời bằng từ khóa
        logger.warning('Vector search failed (%s, code=%s)', type(exc).__name__, getattr(exc, 'code', None))
        vector = []

    # Rerank: Reciprocal Rank Fusion giữa hai danh sách, gộp theo mục (small-to-big).
    scores, meta = {}, {}
    for rank, (article, index, section) in enumerate(keyword):
        key = (article.pk, index)
        scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank)
        meta[key] = (article, section)
    seen, rank = set(), 0
    for chunk in vector:
        key = (chunk.article_id, chunk.section_index)
        if key in seen:
            continue
        seen.add(key)
        scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank)
        rank += 1
        if key not in meta:
            meta[key] = (chunk.article, chunk.article.sections[chunk.section_index])

    results, refs = [], []
    for key in sorted(scores, key=lambda k: (-scores[k], k))[:limit]:
        article, section = meta[key]
        results.append({'article_id': article.pk, 'title': article.title, 'version': article.version,
                        'effective_from': article.effective_from.isoformat(),
                        'source_document': article.source_document,
                        'section': section['heading'], 'text': section['text']})
        ref = {'type': 'help', 'id': article.pk}
        if ref not in refs:
            refs.append(ref)

    refs = refs[:2]
    return {'results': results,
            'detail': '' if results else 'Không tìm thấy nguồn đã xuất bản đủ phù hợp. Không suy diễn chính sách.'}, refs