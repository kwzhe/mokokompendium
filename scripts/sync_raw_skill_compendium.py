#!/usr/bin/env python3
"""Sync localized raw skill rows into skills_compendium.db.

The downstream exporter only writes rows that already exist in
skills_compendium.db. Run this after extracting a new data2.lpk so newly
released classes and newly added class skills are represented before the
description updater/exporter run.
"""

import argparse
import os
import re
import sqlite3
from pathlib import Path


QUICKBMS_DIR = Path(os.environ.get("QUICKBMS_DIR", "/home/poyo/Projects/quickbms"))
TABLE_DIR = QUICKBMS_DIR / "db/data2"
COMPENDIUM_DB = QUICKBMS_DIR / "skills_compendium.db"
PC_DB = TABLE_DIR / "EFGame_Extra\\ClientData\\TableData\\EFTable_PC.db"
SKILL_DB = TABLE_DIR / "EFGame_Extra\\ClientData\\TableData\\EFTable_Skill.db"
SKILL_FEATURE_DB = TABLE_DIR / "EFGame_Extra\\ClientData\\TableData\\EFTable_SkillFeature.db"
GAMEMSG_DB = TABLE_DIR / "EFGame_Extra\\ClientData\\TableData\\EFTable_GameMsg.db"

# Move/Toggle rows are usually alternate states, not standalone guide tooltip
# targets. Existing curated compendium data mostly excludes them.
EXCLUDED_SKILL_TYPES = (11, 12)


def msg(cursor, key):
    if not key:
        return None
    row = cursor.execute(
        'SELECT MSG FROM GameMsg_English WHERE "KEY" = ?',
        (key,),
    ).fetchone()
    return row[0] if row else None


def tier_from_icon_path(icon_path):
    match = re.search(r"Tripod_Tier_(\d+)", icon_path or "")
    return int(match.group(1)) if match else None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync raw localized skill/tripod rows into skills_compendium.db."
    )
    parser.add_argument(
        "--class-id",
        action="append",
        type=int,
        dest="class_ids",
        help="Limit sync to one class id. May be supplied multiple times.",
    )
    parser.add_argument(
        "--replace-class",
        action="store_true",
        help="Delete existing synced class rows before importing selected classes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing to skills_compendium.db.",
    )
    return parser.parse_args()


def released_classes(pc_connection, selected_class_ids):
    query = "SELECT PrimaryKey, Name FROM PC WHERE Released = 1"
    params = []
    if selected_class_ids:
        placeholders = ",".join("?" for _ in selected_class_ids)
        query += f" AND PrimaryKey IN ({placeholders})"
        params.extend(selected_class_ids)
    query += " ORDER BY PrimaryKey"
    return {
        int(class_id): class_name
        for class_id, class_name in pc_connection.execute(query, params).fetchall()
    }


def raw_learn_classes(raw_connection, selected_class_ids):
    query = """
        SELECT DISTINCT LearnClass
        FROM Skill
        WHERE LearnClass > 0
          AND Name LIKE 'tip.name.skill_%'
    """
    params = []
    if selected_class_ids:
        placeholders = ",".join("?" for _ in selected_class_ids)
        query += f" AND LearnClass IN ({placeholders})"
        params.extend(selected_class_ids)
    return {int(row[0]) for row in raw_connection.execute(query, params).fetchall()}


def load_skill_rows(raw_connection, message_connection, class_id):
    rows = raw_connection.execute(
        """
        WITH candidates AS (
          SELECT
            PrimaryKey AS skill_id,
            Name AS name_key,
            Desc AS desc_key,
            Icon AS icon_file,
            IconIndex AS icon_index,
            Type AS type,
            ROW_NUMBER() OVER (
              PARTITION BY PrimaryKey
              ORDER BY
                CASE WHEN SecondaryKey = 1 THEN 0 ELSE 1 END,
                CASE WHEN Desc != '' THEN 0 ELSE 1 END,
                SecondaryKey
            ) AS rn
          FROM Skill
          WHERE LearnClass = ?
            AND Name LIKE 'tip.name.skill_%'
        )
        SELECT skill_id, name_key, desc_key, icon_file, icon_index, type
        FROM candidates
        WHERE rn = 1
          AND type NOT IN (?, ?)
        ORDER BY skill_id
        """,
        (class_id, *EXCLUDED_SKILL_TYPES),
    ).fetchall()

    loaded = []
    for skill_id, name_key, desc_key, icon_file, icon_index, skill_type in rows:
        skill_name = msg(message_connection, name_key)
        if not skill_name:
            continue
        loaded.append(
            {
                "skill_id": int(skill_id),
                "class_id": int(class_id),
                "icon_file": icon_file,
                "icon_index": int(icon_index),
                "skill_name": skill_name,
                "description": msg(message_connection, desc_key),
                "type": int(skill_type),
            }
        )
    return loaded


def load_tripod_rows(feature_connection, message_connection, skill_id):
    rows = feature_connection.execute(
        """
        WITH candidates AS (
          SELECT
            SecondaryKey AS slot_number,
            IconPath AS icon_path,
            IconIndex AS icon_index,
            NameKey AS name_key,
            DescKey AS desc_key,
            ROW_NUMBER() OVER (
              PARTITION BY SecondaryKey
              ORDER BY
                CASE WHEN Level = 1 THEN 0 ELSE 1 END,
                SourceRow DESC
            ) AS rn
          FROM SkillFeature
          WHERE PrimaryKey = ?
            AND NameKey LIKE 'tip.name.tripod_%'
        )
        SELECT slot_number, icon_path, icon_index, name_key, desc_key
        FROM candidates
        WHERE rn = 1
        ORDER BY slot_number
        """,
        (skill_id,),
    ).fetchall()

    loaded = []
    for slot_number, icon_path, icon_index, name_key, desc_key in rows:
        tripod_name = msg(message_connection, name_key)
        if not tripod_name:
            continue
        loaded.append(
            {
                "skill_id": int(skill_id),
                "slot_number": int(slot_number),
                "tier": tier_from_icon_path(icon_path),
                "icon_index": int(icon_index),
                "tripod_name": tripod_name,
                "description": msg(message_connection, desc_key),
            }
        )
    return loaded


def main():
    args = parse_args()

    for path in (COMPENDIUM_DB, PC_DB, SKILL_DB, SKILL_FEATURE_DB, GAMEMSG_DB):
        if not path.exists():
            raise SystemExit(f"Missing required database: {path}")

    comp = sqlite3.connect(COMPENDIUM_DB)
    pc = sqlite3.connect(PC_DB)
    raw = sqlite3.connect(SKILL_DB)
    features = sqlite3.connect(SKILL_FEATURE_DB)
    messages = sqlite3.connect(GAMEMSG_DB)

    try:
        selected_class_ids = set(args.class_ids or [])
        released = released_classes(pc, selected_class_ids)
        learn_classes = raw_learn_classes(raw, selected_class_ids)
        class_ids = sorted(learn_classes & set(released))

        planned = []
        for class_id in class_ids:
            skill_rows = load_skill_rows(raw, messages, class_id)
            tripod_rows = [
                tripod
                for skill in skill_rows
                for tripod in load_tripod_rows(features, messages, skill["skill_id"])
            ]
            planned.append((class_id, released[class_id], skill_rows, tripod_rows))

        if args.dry_run:
            for class_id, class_name, skill_rows, tripod_rows in planned:
                print(
                    f"Would sync {class_id} {class_name}: "
                    f"{len(skill_rows)} skills, {len(tripod_rows)} tripods"
                )
            return

        comp.execute("BEGIN")
        for class_id, class_name, skill_rows, tripod_rows in planned:
            if args.replace_class:
                comp.execute(
                    """
                    DELETE FROM tripods
                    WHERE skill_id IN (
                      SELECT skill_id FROM skills WHERE class_id = ?
                    )
                    """,
                    (class_id,),
                )
                comp.execute("DELETE FROM skills WHERE class_id = ?", (class_id,))
            else:
                for skill in skill_rows:
                    comp.execute(
                        "DELETE FROM tripods WHERE skill_id = ?",
                        (skill["skill_id"],),
                    )

            for skill in skill_rows:
                comp.execute(
                    """
                    INSERT OR REPLACE INTO skills(
                      skill_id, class_id, icon_file, icon_index, skill_name, description
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        skill["skill_id"],
                        skill["class_id"],
                        skill["icon_file"],
                        skill["icon_index"],
                        skill["skill_name"],
                        skill["description"],
                    ),
                )

            for tripod in tripod_rows:
                comp.execute(
                    """
                    INSERT OR REPLACE INTO tripods(
                      skill_id, slot_number, tier, icon_index, tripod_name, description
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tripod["skill_id"],
                        tripod["slot_number"],
                        tripod["tier"],
                        tripod["icon_index"],
                        tripod["tripod_name"],
                        tripod["description"],
                    ),
                )

            print(
                f"Synced {class_id} {class_name}: "
                f"{len(skill_rows)} skills, {len(tripod_rows)} tripods"
            )

        comp.commit()
    except Exception:
        comp.rollback()
        raise
    finally:
        comp.close()
        pc.close()
        raw.close()
        features.close()
        messages.close()


if __name__ == "__main__":
    main()
