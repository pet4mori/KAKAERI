import sqlite3
import os
import hmac
from datetime import datetime, timedelta
from pathlib import Path

from openai import OpenAI, RateLimitError
import pandas as pd
import streamlit as st


DB_PATH = Path(__file__).with_name("kakari.db")
ENV_PATH = Path(__file__).with_name(".env")
STATUS_TODO = "未着手"
STATUS_RUNNING = "作業中"
STATUS_DONE = "完了"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
OPENAI_MODEL = "gpt-5.2"


def get_connection():
    return sqlite3.connect(DB_PATH)


def load_openai_api_key():
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        return api_key

    if not ENV_PATH.exists():
        return None

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key.strip() == "OPENAI_API_KEY":
            return value.strip().strip('"').strip("'")

    return None


def load_app_password():
    password = os.getenv("APP_PASSWORD")
    if password:
        return password

    try:
        password = st.secrets.get("APP_PASSWORD")
    except Exception:
        password = None

    if password:
        return str(password)

    if not ENV_PATH.exists():
        return None

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key.strip() == "APP_PASSWORD":
            return value.strip().strip('"').strip("'")

    return None


def require_password():
    app_password = load_app_password()
    if not app_password:
        st.error("APP_PASSWORD が設定されていません。")
        st.info("Streamlit Secrets または .env に APP_PASSWORD を設定してください。")
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.title("KAKARI")
    password = st.text_input("パスワード", type="password")

    if st.button("ログイン"):
        if hmac.compare_digest(password, app_password):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが違います。")

    st.stop()


def now_text():
    return datetime.now().strftime(DATETIME_FORMAT)


def parse_datetime(value):
    return datetime.strptime(value, DATETIME_FORMAT)


def has_timer_started(value):
    return isinstance(value, str) and value.strip() != ""


def elapsed_hours_since(started_at):
    if not has_timer_started(started_at):
        return 0

    return (datetime.now() - parse_datetime(started_at)).total_seconds() / 3600


def add_column_if_missing(conn, table_name, column_name, column_definition):
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    column_names = [column[1] for column in columns]

    if column_name not in column_names:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                estimated_hours REAL NOT NULL,
                category TEXT NOT NULL,
                memo TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        add_column_if_missing(conn, "tasks", "actual_hours", "REAL")
        add_column_if_missing(
            conn, "tasks", "status", f"TEXT NOT NULL DEFAULT '{STATUS_TODO}'"
        )
        add_column_if_missing(conn, "tasks", "completed_at", "TEXT")
        add_column_if_missing(conn, "tasks", "reason", "TEXT")
        add_column_if_missing(conn, "tasks", "timer_started_at", "TEXT")
        normalize_statuses(conn)


def normalize_statuses(conn):
    conn.execute(
        """
        UPDATE tasks
        SET status = ?
        WHERE completed_at IS NOT NULL
        """,
        (STATUS_DONE,),
    )
    conn.execute(
        """
        UPDATE tasks
        SET status = ?
        WHERE completed_at IS NULL
            AND timer_started_at IS NULL
        """,
        (STATUS_TODO,),
    )
    conn.execute(
        """
        UPDATE tasks
        SET status = ?
        WHERE completed_at IS NULL
            AND timer_started_at IS NOT NULL
        """,
        (STATUS_RUNNING,),
    )


def add_task(name, estimated_hours, category, memo):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                name,
                estimated_hours,
                category,
                memo,
                created_at,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, estimated_hours, category, memo, now_text(), STATUS_TODO),
        )


def get_running_task(conn):
    return conn.execute(
        """
        SELECT id, name, actual_hours, timer_started_at
        FROM tasks
        WHERE status = ?
            AND timer_started_at IS NOT NULL
        LIMIT 1
        """,
        (STATUS_RUNNING,),
    ).fetchone()


def stop_running_task(conn):
    running_task = get_running_task(conn)
    if running_task is None:
        return None

    task_id, task_name, actual_hours, timer_started_at = running_task
    elapsed_hours = elapsed_hours_since(timer_started_at)
    new_actual_hours = round((actual_hours or 0) + elapsed_hours, 4)

    conn.execute(
        """
        UPDATE tasks
        SET actual_hours = ?,
            status = ?,
            timer_started_at = NULL
        WHERE id = ?
        """,
        (new_actual_hours, STATUS_TODO, task_id),
    )

    return {
        "id": task_id,
        "name": task_name,
        "elapsed_hours": elapsed_hours,
        "actual_hours": new_actual_hours,
    }


def start_timer(task_id):
    with get_connection() as conn:
        stopped_task = stop_running_task(conn)
        conn.execute(
            """
            UPDATE tasks
            SET status = ?,
                timer_started_at = ?
            WHERE id = ?
                AND completed_at IS NULL
            """,
            (STATUS_RUNNING, now_text(), task_id),
        )

    return stopped_task


def stop_timer():
    with get_connection() as conn:
        return stop_running_task(conn)


def complete_task_with_timer(task_id, actual_hours, reason):
    stopped_task = None

    with get_connection() as conn:
        running_task = get_running_task(conn)
        if running_task is not None and running_task[0] == task_id:
            stopped_task = stop_running_task(conn)
            actual_hours = stopped_task["actual_hours"]

        conn.execute(
            """
            UPDATE tasks
            SET actual_hours = ?,
                status = ?,
                completed_at = ?,
                reason = ?,
                timer_started_at = NULL
            WHERE id = ?
            """,
            (actual_hours, STATUS_DONE, now_text(), reason, task_id),
        )

    return actual_hours, stopped_task


def load_tasks():
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                id AS ID,
                name AS タスク名,
                estimated_hours AS 予定時間,
                actual_hours AS 実績時間,
                CASE
                    WHEN actual_hours IS NULL THEN NULL
                    ELSE ROUND(actual_hours - estimated_hours, 2)
                END AS 差分,
                status AS ステータス,
                category AS カテゴリ,
                reason AS ズレた理由,
                memo AS メモ,
                created_at AS 登録日時,
                completed_at AS 完了日時
            FROM tasks
            ORDER BY id DESC
            """,
            conn,
        )


def load_incomplete_tasks():
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                id,
                name,
                estimated_hours,
                actual_hours,
                status,
                timer_started_at
            FROM tasks
            WHERE status != ?
            ORDER BY id DESC
            """,
            conn,
            params=(STATUS_DONE,),
        )


def load_completed_tasks():
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                id,
                name,
                estimated_hours,
                actual_hours,
                ROUND(actual_hours - estimated_hours, 2) AS diff_hours,
                category,
                reason,
                completed_at
            FROM tasks
            WHERE status = ?
                AND actual_hours IS NOT NULL
            ORDER BY completed_at DESC
            """,
            conn,
            params=(STATUS_DONE,),
        )


def load_completed_tasks_between(start_date, end_date):
    start_text = f"{start_date.strftime('%Y-%m-%d')} 00:00:00"
    end_text = f"{end_date.strftime('%Y-%m-%d')} 23:59:59"

    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                name AS タスク名,
                category AS カテゴリ,
                estimated_hours AS 予定時間,
                actual_hours AS 実績時間,
                ROUND(actual_hours - estimated_hours, 2) AS 差分,
                reason AS ズレた理由,
                completed_at AS 完了日時
            FROM tasks
            WHERE status = ?
                AND actual_hours IS NOT NULL
                AND completed_at BETWEEN ? AND ?
            ORDER BY completed_at DESC
            """,
            conn,
            params=(STATUS_DONE, start_text, end_text),
        )


def load_running_task():
    with get_connection() as conn:
        running_task = get_running_task(conn)

    if running_task is None:
        return None

    task_id, task_name, actual_hours, timer_started_at = running_task
    elapsed_hours = elapsed_hours_since(timer_started_at)

    return {
        "id": task_id,
        "name": task_name,
        "actual_hours": actual_hours or 0,
        "timer_started_at": timer_started_at,
        "elapsed_hours": elapsed_hours,
    }


def build_ai_report_prompt(report_tasks, start_date, end_date):
    report_rows = []

    for row in report_tasks.itertuples(index=False):
        reason = getattr(row, "ズレた理由") or "未入力"
        report_rows.append(
            {
                "タスク名": getattr(row, "タスク名"),
                "カテゴリ": getattr(row, "カテゴリ"),
                "予定時間": getattr(row, "予定時間"),
                "実績時間": getattr(row, "実績時間"),
                "差分": getattr(row, "差分"),
                "ズレた理由": reason,
                "完了日時": getattr(row, "完了日時"),
            }
        )

    return f"""
あなたは個人開発者の工数管理を支援するコーチです。
以下は KAKARI で記録した {start_date} から {end_date} までの完了タスクです。

目的:
- 予定時間と実績時間のズレの傾向を分析する
- ズレた理由から、来週の具体的な改善案を出す
- 責める口調ではなく、次に活かせる実務的な口調にする

出力形式:
1. 今週の傾向
2. ズレが大きかった要因
3. 来週の改善案
4. 次に記録するとよさそうなこと

タスクデータ:
{report_rows}
""".strip()


def analyze_weekly_report(report_tasks, start_date, end_date):
    api_key = load_openai_api_key()
    if not api_key:
        raise ValueError("OPENAI_API_KEY が設定されていません。")

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=build_ai_report_prompt(report_tasks, start_date, end_date),
    )

    return response.output_text


def to_csv_bytes(dataframe):
    return dataframe.to_csv(index=False).encode("utf-8-sig")


st.set_page_config(page_title="KAKARI", page_icon="⏱", layout="wide")
require_password()
init_db()

st.title("KAKARI")
st.caption("自分専用の工数管理・振り返りアプリ")

st.header("タスク登録")

category_options = ["実装", "調査", "レビュー対応", "資料作成", "環境構築", "その他"]

with st.form("task_form", clear_on_submit=True):
    task_name = st.text_input("タスク名")
    estimated_hours = st.number_input(
        "予定時間（時間）",
        min_value=0.25,
        max_value=24.0,
        value=1.0,
        step=0.25,
    )
    category = st.selectbox("カテゴリ", category_options)
    memo = st.text_area("メモ", height=100)

    submitted = st.form_submit_button("登録する")

if submitted:
    if not task_name.strip():
        st.error("タスク名を入力してください。")
    else:
        add_task(task_name.strip(), estimated_hours, category, memo.strip())
        st.success("タスクを登録しました。")

st.header("タイマー")

incomplete_tasks = load_incomplete_tasks()
running_task = load_running_task()

if incomplete_tasks.empty:
    st.info("計測できる未完了タスクはありません。")
else:
    if running_task is None:
        st.info("現在計測中のタスクはありません。")
    else:
        total_hours = running_task["actual_hours"] + running_task["elapsed_hours"]
        st.success(
            f"計測中: {running_task['name']} / 現在の実績時間 {total_hours:.2f} 時間"
        )

    for row in incomplete_tasks.itertuples(index=False):
        col_task, col_time, col_action = st.columns([4, 2, 2])

        with col_task:
            st.write(f"**{row.name}**")
            st.caption(f"予定 {row.estimated_hours} 時間 / {row.status}")

        with col_time:
            actual_hours = row.actual_hours or 0
            actual_hours += elapsed_hours_since(row.timer_started_at)
            st.write(f"{actual_hours:.2f} 時間")

        with col_action:
            if row.status == STATUS_RUNNING:
                if st.button("停止", key=f"stop_{row.id}"):
                    stopped_task = stop_timer()
                    if stopped_task:
                        st.success(
                            f"{stopped_task['name']} を停止しました。"
                            f" 実績時間は {stopped_task['actual_hours']:.2f} 時間です。"
                        )
                    st.rerun()
            else:
                if st.button("開始", key=f"start_{row.id}"):
                    stopped_task = start_timer(row.id)
                    if stopped_task:
                        st.info(
                            f"{stopped_task['name']} を自動停止してから開始しました。"
                        )
                    st.rerun()

st.header("タスク完了")

incomplete_tasks = load_incomplete_tasks()

if incomplete_tasks.empty:
    st.info("完了にできる未完了タスクはありません。")
else:
    task_options = {
        f"{row.name}（予定 {row.estimated_hours} 時間）": row.id
        for row in incomplete_tasks.itertuples(index=False)
    }

    with st.form("complete_form"):
        selected_task_label = st.selectbox("完了するタスク", task_options.keys())
        selected_task_id = task_options[selected_task_label]
        selected_task = incomplete_tasks[
            incomplete_tasks["id"] == selected_task_id
        ].iloc[0]
        default_actual_hours = selected_task["actual_hours"] or 0.0

        actual_hours = st.number_input(
            "実績時間（時間）",
            min_value=0.0,
            max_value=24.0,
            value=float(round(default_actual_hours, 2)),
            step=0.25,
        )
        reason = st.text_area(
            "ズレた理由（任意）",
            placeholder="例：調査に時間がかかった、仕様確認が必要だった、集中できたので早く終わった",
            height=100,
        )
        completed = st.form_submit_button("完了にする")

    if completed:
        final_actual_hours, stopped_task = complete_task_with_timer(
            selected_task_id, actual_hours, reason.strip()
        )
        diff = round(final_actual_hours - selected_task["estimated_hours"], 2)

        if stopped_task:
            st.info("計測中だったため、タイマーを自動停止してから完了しました。")

        st.success("お疲れ様でした。タスクを完了にしました。")
        st.write(f"予定時間: {selected_task['estimated_hours']} 時間")
        st.write(f"実績時間: {final_actual_hours:.2f} 時間")
        st.write(f"差分: {diff:.2f} 時間")
        if reason.strip():
            st.write(f"ズレた理由: {reason.strip()}")

st.header("集計")

completed_tasks = load_completed_tasks()

if completed_tasks.empty:
    st.info("完了済みタスクがまだありません。タスクを完了すると集計が表示されます。")
else:
    completed_count = len(completed_tasks)
    total_estimated_hours = completed_tasks["estimated_hours"].sum()
    total_actual_hours = completed_tasks["actual_hours"].sum()
    total_diff_hours = total_actual_hours - total_estimated_hours

    col_count, col_estimated, col_actual, col_diff = st.columns(4)
    col_count.metric("完了タスク数", f"{completed_count} 件")
    col_estimated.metric("合計予定時間", f"{total_estimated_hours:.2f} 時間")
    col_actual.metric("合計実績時間", f"{total_actual_hours:.2f} 時間")
    col_diff.metric("合計差分", f"{total_diff_hours:.2f} 時間")

    category_summary = (
        completed_tasks.groupby("category", as_index=False)["actual_hours"]
        .sum()
        .rename(columns={"category": "カテゴリ", "actual_hours": "実績時間"})
        .sort_values("実績時間", ascending=False)
    )

    st.subheader("カテゴリ別の実績時間")
    st.dataframe(category_summary, width="stretch", hide_index=True)
    st.bar_chart(category_summary, x="カテゴリ", y="実績時間")

st.header("今週のレポート")

today = datetime.now().date()
week_start = today - timedelta(days=today.weekday())

col_start, col_end = st.columns(2)
with col_start:
    report_start_date = st.date_input("開始日", value=week_start)
with col_end:
    report_end_date = st.date_input("終了日", value=today)

if report_start_date > report_end_date:
    st.error("開始日は終了日より前の日付にしてください。")
else:
    weekly_tasks = load_completed_tasks_between(report_start_date, report_end_date)

    if weekly_tasks.empty:
        st.info("この期間に完了したタスクはありません。")
    else:
        weekly_count = len(weekly_tasks)
        weekly_estimated_hours = weekly_tasks["予定時間"].sum()
        weekly_actual_hours = weekly_tasks["実績時間"].sum()
        weekly_diff_hours = weekly_actual_hours - weekly_estimated_hours

        col_count, col_estimated, col_actual, col_diff = st.columns(4)
        col_count.metric("完了タスク数", f"{weekly_count} 件")
        col_estimated.metric("予定時間", f"{weekly_estimated_hours:.2f} 時間")
        col_actual.metric("実績時間", f"{weekly_actual_hours:.2f} 時間")
        col_diff.metric("差分", f"{weekly_diff_hours:.2f} 時間")

        st.subheader("完了タスク")
        st.dataframe(weekly_tasks, width="stretch", hide_index=True)
        st.download_button(
            "この期間のタスクをCSVでダウンロード",
            data=to_csv_bytes(weekly_tasks),
            file_name=(
                f"kakari_report_{report_start_date.strftime('%Y%m%d')}_"
                f"{report_end_date.strftime('%Y%m%d')}.csv"
            ),
            mime="text/csv",
        )

        reasons = weekly_tasks["ズレた理由"].dropna()
        reasons = reasons[reasons.str.strip() != ""]

        st.subheader("ズレた理由")
        if reasons.empty:
            st.info("この期間のズレた理由はまだ入力されていません。")
        else:
            for reason in reasons:
                st.write(f"- {reason}")

        st.subheader("AI分析")
        if st.button("AIで週次レポートを分析する"):
            try:
                with st.spinner("AIが週次レポートを分析しています..."):
                    ai_report = analyze_weekly_report(
                        weekly_tasks, report_start_date, report_end_date
                    )
                st.markdown(ai_report)
            except ValueError as error:
                st.warning(str(error))
                st.info(
                    "`.env.example` を参考に `.env` ファイルを作り、"
                    "`OPENAI_API_KEY=sk-...` を設定してください。"
                )
            except RateLimitError:
                st.warning("OpenAI APIの利用枠または課金設定に問題があります。")
                st.info(
                    "OpenAI Platformで、利用上限、残高、Billing設定を確認してください。"
                    "設定後、少し時間を置いてからもう一度実行してください。"
                )
            except Exception as error:
                st.error("AI分析中にエラーが発生しました。")
                st.exception(error)

st.header("タスク一覧")

tasks = load_tasks()

if tasks.empty:
    st.info("まだタスクが登録されていません。まずは1件登録してみましょう。")
else:
    st.dataframe(tasks, width="stretch", hide_index=True)
    st.download_button(
        "全タスクをCSVでダウンロード",
        data=to_csv_bytes(tasks),
        file_name="kakari_tasks.csv",
        mime="text/csv",
    )
