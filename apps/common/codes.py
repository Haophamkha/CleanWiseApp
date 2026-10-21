import secrets

from django.utils import timezone

# Crockford Base32: bỏ I, L, O, U để khỏi nhầm khi đọc hoặc gõ.
CODE_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


def generate_code(prefix, exists, length=5, when=None):
    """exists: hàm nhận code, trả True nếu đã có trong DB. when: mốc thời gian lấy ngày (mặc định bây giờ)."""
    day = timezone.localtime(when).strftime('%y%m%d')
    for _ in range(5):
        suffix = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(length))
        code = f'{prefix}-{day}-{suffix}'
        if not exists(code):
            return code
    raise RuntimeError(f'Không tạo được mã {prefix}.')