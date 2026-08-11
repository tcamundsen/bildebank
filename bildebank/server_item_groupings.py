from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import db
from .openclip import connect_openclip_db_read_only, openclip_db_path
from .sidecar_paths import regular_database_file_exists


@dataclass(frozen=True)
class ItemGroupingMembership:
    run_id: int
    cluster_id: int
    display_order: int
    algorithm: str
    active_member_count: int


def item_grouping_memberships(
    target: Path,
    file_id: int,
) -> tuple[ItemGroupingMembership, ...]:
    if not regular_database_file_exists(openclip_db_path(target)):
        return ()

    validation_conn = connect_openclip_db_read_only(target, full=False)
    validation_conn.close()
    conn = db.connect_read_only(target)
    try:
        uri = f"{openclip_db_path(target).resolve().as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS openclip_db", (uri,))
        return tuple(
            ItemGroupingMembership(
                run_id=int(row["run_id"]),
                cluster_id=int(row["cluster_id"]),
                display_order=int(row["display_order"]),
                algorithm=str(row["algorithm"]),
                active_member_count=int(row["active_member_count"]),
            )
            for row in conn.execute(
                """
                SELECT
                    runs.id AS run_id,
                    clusters.id AS cluster_id,
                    clusters.display_order,
                    runs.algorithm,
                    COUNT(files.id) AS active_member_count
                FROM openclip_db.image_cluster_members AS item_membership
                JOIN openclip_db.image_clusters AS clusters
                  ON clusters.id = item_membership.cluster_id
                 AND clusters.run_id = item_membership.run_id
                JOIN openclip_db.image_clustering_runs AS runs
                  ON runs.id = item_membership.run_id
                LEFT JOIN openclip_db.image_cluster_members AS members
                  ON members.cluster_id = clusters.id
                 AND members.run_id = clusters.run_id
                LEFT JOIN files
                  ON files.id = members.file_id
                 AND files.deleted_at IS NULL
                WHERE item_membership.file_id = ?
                  AND runs.status = 'completed'
                  AND clusters.kind = 'cluster'
                GROUP BY runs.id, clusters.id
                ORDER BY runs.id DESC, clusters.display_order, clusters.id
                """,
                (file_id,),
            )
        )
    finally:
        conn.close()


def grouping_algorithm_label(algorithm: str) -> str:
    return {
        "minibatch_kmeans": "MiniBatchKMeans",
        "hdbscan": "HDBSCAN",
        "leiden": "Leiden",
    }.get(algorithm, algorithm)
