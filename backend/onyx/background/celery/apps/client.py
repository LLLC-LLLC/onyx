from celery import Celery

import onyx.background.celery.apps.app_base as app_base
from onyx.db.skybase_shared_supabase import assert_worker_app_allowed

assert_worker_app_allowed("client")

celery_app = Celery(__name__)
celery_app.config_from_object("onyx.background.celery.configs.client")
celery_app.Task = app_base.TenantAwareTask  # ty: ignore[invalid-assignment]
