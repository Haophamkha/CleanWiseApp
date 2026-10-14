import django.db.models.deletion
import pgvector.django
from django.db import migrations, models
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    dependencies = [('chatbot', '0003_helparticle_chatbot_help_one_published')]

    operations = [
        VectorExtension(),
        migrations.CreateModel(
            name='HelpChunk',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('section_index', models.PositiveIntegerField()),
                ('part_index', models.PositiveIntegerField(default=0)),
                ('heading', models.CharField(blank=True, max_length=250)),
                ('text', models.TextField()),
                ('content_hash', models.CharField(max_length=64)),
                ('embedding', pgvector.django.VectorField(dimensions=768)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('article', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name='chunks', to='chatbot.helparticle')),
            ],
            options={'ordering': ['article_id', 'section_index', 'part_index']},
        ),
        migrations.AddConstraint(
            model_name='helpchunk',
            constraint=models.UniqueConstraint(fields=('article', 'section_index', 'part_index'),
                                               name='chatbot_chunk_unique'),
        ),
    ]