from celery import shared_task


@shared_task
def sync_withdraw_payouts_task():
    from apps.wallets.withdraw_service import sync_processing_withdraws
    return sync_processing_withdraws()


@shared_task
def expire_topups_task():
    from apps.wallets.topup_service import expire_pending_topups
    return expire_pending_topups()


@shared_task
def alert_stuck_withdraws_task():
    """Ghi log ERROR cho lệnh rút cần đối soát / treo quá lâu (Sentry hoặc Telegram bắt log ERROR)."""
    from apps.wallets.withdraw_service import alert_stuck_withdraws
    return list(alert_stuck_withdraws())

@shared_task
def reconcile_wallets_task():
    from apps.wallets.reconcile import reconcile_wallets
    return reconcile_wallets()