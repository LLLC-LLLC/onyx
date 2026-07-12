from sqlalchemy import text

from onyx.db.engine.async_sql_engine import get_sqlalchemy_async_engine
from onyx.db.engine.sql_engine import get_sqlalchemy_engine
from onyx.db.skybase_shared_supabase import is_shared_supabase_profile
from onyx.db.skybase_shared_supabase import SharedSupabaseContractError


async def warm_up_connections(
    sync_connections_to_warm_up: int = 20, async_connections_to_warm_up: int = 20
) -> None:
    if is_shared_supabase_profile():
        # The v1 profile runs with a small, fixed pool budget.  Its normal
        # launch path sets SKIP_WARM_UP=true; rejecting a direct call keeps the
        # legacy 20+20 defaults from consuming every pool slot during startup.
        if sync_connections_to_warm_up or async_connections_to_warm_up:
            raise SharedSupabaseContractError(
                "Connection warmup is disabled in the shared-Supabase profile."
            )
        return
    sync_postgres_engine = get_sqlalchemy_engine()
    connections = [
        sync_postgres_engine.connect() for _ in range(sync_connections_to_warm_up)
    ]
    for conn in connections:
        conn.execute(text("SELECT 1"))
    for conn in connections:
        conn.close()

    async_postgres_engine = get_sqlalchemy_async_engine()
    async_connections = [
        await async_postgres_engine.connect()
        for _ in range(async_connections_to_warm_up)
    ]
    for async_conn in async_connections:
        await async_conn.execute(text("SELECT 1"))
    for async_conn in async_connections:
        await async_conn.close()
