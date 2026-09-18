"""What the stored threads need to resume under refunds_v2."""

from graphlock import rename_node, set_default

MIGRATIONS = [
    rename_node("wait_for_manager_approval", "manager_review"),
    set_default("currency", "USD"),  # every refund before this deploy was in dollars
]
