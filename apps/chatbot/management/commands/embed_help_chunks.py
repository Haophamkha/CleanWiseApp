import hashlib

from django.core.management.base import BaseCommand

from apps.chatbot.models import HelpChunk
from apps.chatbot.services.embedding import chunk_section, embed_texts
from apps.chatbot.services.knowledge import published_articles


class Command(BaseCommand):
    help = 'Chia chunk và embedding các bài hướng dẫn đã xuất bản vào pgvector.'

    def add_arguments(self, parser):
        parser.add_argument('--rebuild', action='store_true', help='Xóa hết chunk và embed lại')

    def handle(self, *args, **options):
        if options['rebuild']:
            HelpChunk.objects.all().delete()
        live = list(published_articles())
        HelpChunk.objects.exclude(article__in=[a.pk for a in live]).delete()
        for article in live:
            wanted = {}
            for si, section in enumerate(article.sections):
                for pi, part in enumerate(chunk_section(section['text'])):
                    wanted[(si, pi)] = (section['heading'], part,
                                        f"{article.title} - {section['heading']}\n{part}")
            existing = {(c.section_index, c.part_index): c for c in article.chunks.all()}
            stale = [c.pk for key, c in existing.items() if key not in wanted]
            if stale:
                HelpChunk.objects.filter(pk__in=stale).delete()
            todo = []
            for key, (heading, part, content) in wanted.items():
                digest = hashlib.sha256(content.encode()).hexdigest()
                if key in existing and existing[key].content_hash == digest:
                    continue
                todo.append((key, heading, part, content, digest))
            if todo:
                vectors = embed_texts([t[3] for t in todo], 'RETRIEVAL_DOCUMENT')
                for (key, heading, part, _, digest), vec in zip(todo, vectors):
                    HelpChunk.objects.update_or_create(
                        article=article, section_index=key[0], part_index=key[1],
                        defaults={'heading': heading, 'text': part, 'content_hash': digest, 'embedding': vec})
            self.stdout.write(f'{article.slug} v{article.version}: {len(wanted)} chunk, embed mới {len(todo)}')