"""Shared catalog-mutation primitives for the job handlers (§4, §6).

The forget/delete rules are identical wherever a file instance disappears — a
plain filesystem delete is not trash (§6), so an ``active`` asset that loses its
last instance is forgotten entirely (its fingerprints cascade), while a ``trashed``
asset is kept at zero instances (its fingerprint is the trash memory). scan,
dedup, and cleanup all apply these, so they live here rather than being copied per
job module.
"""

from __future__ import annotations


def delete_instance(conn, instance_id: int) -> None:
    """Drop a single ``file_instances`` row (presence = row existence, §4)."""
    conn.execute("DELETE FROM file_instances WHERE id=?", (instance_id,))


def forget_if_orphaned(conn, asset_id: int) -> None:
    """Delete an ``active`` asset that now has zero file instances (§4 forget rule).

    A ``trashed`` asset is kept at zero instances (its fingerprint is trash memory).
    Deleting the asset cascades its ``phash``/``vphash``/``embeddings``/
    ``similarity_edges`` rows (``ON DELETE CASCADE``).
    """
    n = conn.execute(
        "SELECT COUNT(*) c FROM file_instances WHERE asset_id=?", (asset_id,)
    ).fetchone()["c"]
    if n:
        return
    st = conn.execute("SELECT status FROM assets WHERE id=?", (asset_id,)).fetchone()
    if st is not None and st["status"] == "active":
        conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))  # cascade fingerprints
