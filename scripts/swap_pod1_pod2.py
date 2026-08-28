"""
One-time data-correction script: swap all data between PODS 1 and PODS 2.

Why: the team originally seeded as "PODS 1" in this app (Anisa Rahmy, Arie
Prabowo, Ashari, Dimas) turned out to actually be the real PODS 2 team. The
real PODS 1 team's opportunities live in a separate spreadsheet import. This
script swaps every pod='pods1' row to pod='pods2' and vice versa, across
deals, deal_tasks, users, login_logs, and performance, and swaps the two
PODS' own config figures (target/achievement/AM maps) - never the shared
Stages/Pillars taxonomy, which isn't POD-specific.

Safe to re-run: running it twice just swaps back. Uses a single UPDATE with
a CASE expression per table (never an invalid intermediate value), except for
`config`, which has a UNIQUE(pod) index - there the DATA columns are swapped
between the two existing rows instead of touching `pod` itself, so no
uniqueness collision is possible.

Usage:
    python3 scripts/swap_pod1_pod2.py [path-to-db.sqlite3]

If no path is given, defaults to db.sqlite3 next to app.py (same default the
app itself uses).
"""
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def swap(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    before = {
        "deals": dict(conn.execute(
            "SELECT pod, COUNT(*) FROM deals WHERE pod IN ('pods1','pods2') GROUP BY pod").fetchall()),
        "users": dict(conn.execute(
            "SELECT pod, COUNT(*) FROM users WHERE pod IN ('pods1','pods2') GROUP BY pod").fetchall()),
    }

    for table in ("deals", "deal_tasks", "users", "login_logs", "performance"):
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not has_table:
            continue
        conn.execute(
            f"""UPDATE {table} SET pod = CASE pod
                    WHEN 'pods1' THEN 'pods2'
                    WHEN 'pods2' THEN 'pods1'
                    ELSE pod END
                WHERE pod IN ('pods1', 'pods2')"""
        )

    # config: swap the DATA between the two rows, leave each row's `pod` (and
    # the separate shared pod=NULL taxonomy row) untouched - avoids ever
    # having two rows with the same `pod` value at once (UNIQUE constraint).
    row1 = conn.execute("SELECT * FROM config WHERE pod = 'pods1'").fetchone()
    row2 = conn.execute("SELECT * FROM config WHERE pod = 'pods2'").fetchone()
    if row1 and row2:
        fields = ("target_amount", "am_targets", "am_achievements", "am_recurring",
                   "current_achievement", "recurring_revenue")
        conn.execute(
            f"UPDATE config SET {', '.join(f'{f} = ?' for f in fields)} WHERE id = ?",
            tuple(row2[f] for f in fields) + (row1["id"],),
        )
        conn.execute(
            f"UPDATE config SET {', '.join(f'{f} = ?' for f in fields)} WHERE id = ?",
            tuple(row1[f] for f in fields) + (row2["id"],),
        )

    # Cosmetic: the bootstrap admin usernames ("pods1_admin"/"pods2_admin") now sit in
    # the other POD after the swap above - rename them to match where they actually
    # live now, so nobody's confused later. Only renames if the target name is free.
    for old_name, new_name in (("pods1_admin", "pods2_admin"), ("pods2_admin", "pods1_admin")):
        row = conn.execute("SELECT id FROM users WHERE username = ?", (old_name,)).fetchone()
        clash = conn.execute("SELECT id FROM users WHERE username = ?", (new_name,)).fetchone()
        if row and not clash:
            conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_name, row["id"]))

    conn.commit()

    after = {
        "deals": dict(conn.execute(
            "SELECT pod, COUNT(*) FROM deals WHERE pod IN ('pods1','pods2') GROUP BY pod").fetchall()),
        "users": dict(conn.execute(
            "SELECT pod, COUNT(*) FROM users WHERE pod IN ('pods1','pods2') GROUP BY pod").fetchall()),
    }
    conn.close()
    print("Before:", before)
    print("After: ", after)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "db.sqlite3")
    if not os.path.exists(path):
        print(f"No database found at {path}")
        sys.exit(1)
    swap(path)
