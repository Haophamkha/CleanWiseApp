import logging
from decimal import Decimal

from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from .models import Wallet, WalletTransaction

logger = logging.getLogger(__name__)
_DEC = DecimalField(max_digits=14, decimal_places=2)


def reconcile_wallets():
    T = WalletTransaction
    is_debit = Q(transactions__direction=T.Direction.DEBIT) | Q(
        transactions__direction='',
        transactions__type__in=[T.Type.PAYMENT, T.Type.WITHDRAW],
    )
    signed = Case(
        When(is_debit, then=-F('transactions__amount')),
        default=F('transactions__amount'),
        output_field=_DEC,
    )
    ledger = Coalesce(
        Sum(signed, filter=Q(transactions__status__in=[T.Status.SUCCESS, T.Status.PENDING])),
        Value(Decimal('0')), output_field=_DEC,
    )
    bad = list(
        Wallet.objects.annotate(ledger=ledger).exclude(balance=F('ledger'))
        .values_list('id', 'user_id', 'balance', 'ledger')
    )
    for wid, uid, bal, led in bad:
        logger.error('LỆCH VÍ wallet=%s user=%s balance=%s sổ_cái=%s', wid, uid, bal, led)
    return len(bad)