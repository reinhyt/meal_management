import os
import json
from datetime import datetime
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import plotly.express as px

# ----------------------------------------------------
# 設定情報
# ----------------------------------------------------
CREDENTIALS_FILE = "credentials.json"
# 💡 ここにご自身のスプレッドシートIDを確実に貼り付けてください
SPREADSHEET_ID = "18hsGix3rrv2QSVtfkrPaSwxLHUuiS1noczGqRUBrh-0"

# 💡 トレーニングメニュー定義(種目, セット数)
TRAINING_PLAN = {
    "A": [("チェストプレス", 3), ("ショルダープレス", 2), ("レッグプレス", 4), ("ディップス", 1), ("腹筋マシン", 2)],
    "B": [("ラットプルダウン", 4), ("アブダクション", 4), ("アームカール", 2), ("腹筋マシン", 2)],
}
# 重量を記録せず回数のみで管理する種目
REPS_ONLY_EXERCISES = {"腹筋マシン"}
# 各種目が効く部位と負荷の配分(主働筋1.0・補助筋0.5)。総負荷量を部位別に按分する際に使用
MUSCLE_MAP = {
    "チェストプレス": {"胸": 1.0, "三頭": 0.5, "肩": 0.5},
    "ショルダープレス": {"肩": 1.0, "三頭": 0.5},
    "ディップス": {"三頭": 1.0, "胸": 0.5, "肩": 0.5},
    "ラットプルダウン": {"背中": 1.0, "二頭": 0.5},
    "アームカール": {"二頭": 1.0},
    "レッグプレス": {"脚": 1.0},
    "アブダクション": {"外転筋": 1.0},
    "腹筋マシン": {"腹筋": 1.0},
}

class UltimateSheetsDietAppV4:
    def __init__(self):
        self.target = {"calories": 2000, "protein": 120, "fat": 50, "carbohydrate": 255, "salt": 7.0}
        self.slots = ["朝食", "昼食", "夕食", "間食"]
        self.food_master = {}
        self.presets = {}
        self.history = {}
        self.training_df = pd.DataFrame(columns=["日付", "Day", "種目", "セット", "重量(kg)", "回数"])
        
        # Streamlit CloudのSecrets（クラウド）とローカルファイルを自動判別
        creds_info = None
        if "gcp_service_account" in st.secrets:
            raw_secret = st.secrets["gcp_service_account"]
            if isinstance(raw_secret, str):
                try:
                    creds_info = json.loads(raw_secret)
                except Exception:
                    # 改行文字が含まれている場合のフォールバック処理
                    creds_info = json.loads(raw_secret, strict=False)
            else:
                # すでに辞書（dict）型として読み込まれている場合
                creds_info = dict(raw_secret)
        elif os.path.exists(CREDENTIALS_FILE):
            with open(CREDENTIALS_FILE, "r") as f:
                creds_info = json.load(f)

        if creds_info and SPREADSHEET_ID != "ここにあなたのスプレッドシートIDを貼り付けてください":
            try:
                scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
                self.gc = gspread.authorize(creds)
                self.sh = self.gc.open_by_key(SPREADSHEET_ID)
                self.migrate_history_if_needed()
                self.load_data_from_sheets()
            except Exception as e:
                st.error(f"⚠️ スプレッドシートへの接続に失敗しました: {e}")

    # 💡 API制限回避用のキャッシュ
    def load_data_from_sheets(self):
        @st.cache_data(ttl=600, show_spinner=False)
        def _fetch_all_sheets_data(_sh_key):
            sh = self.gc.open_by_key(_sh_key)
            m_data = []
            try: m_data = sh.worksheet("master").get_all_records()
            except Exception: pass
            tp_data = []
            try: tp_data = sh.worksheet("target_preset").get_all_records()
            except Exception: pass
            h_data = []
            try: h_data = sh.worksheet("history").get_all_records()
            except Exception: pass
            tr_data = []
            try: tr_data = sh.worksheet("training").get_all_records()
            except Exception: pass
            return {"master": m_data, "target_preset": tp_data, "history": h_data, "training": tr_data}

        if st.session_state.get("clear_cache", False):
            st.cache_data.clear()
            st.session_state["clear_cache"] = False

        try:
            raw = _fetch_all_sheets_data(SPREADSHEET_ID)
            self.food_master = {}
            for r in raw["master"]:
                if not r.get('食材名'): continue
                self.food_master[str(r['食材名'])] = {
                    "unit_type": "g" if r.get('単位') == "100gあたり" or r.get('単位') == "g" else "count",
                    "calories": float(r.get('カロリー', 0) if r.get('カロリー') != '' else 0),
                    "protein": float(r.get('タンパク質', 0) if r.get('タンパク質') != '' else 0),
                    "fat": float(r.get('脂質', 0) if r.get('脂質') != '' else 0),
                    "carbohydrate": float(r.get('炭水化物', 0) if r.get('炭水化物') != '' else 0),
                    "salt": float(r.get('塩分', 0) if r.get('塩分') != '' and r.get('塩分') is not None else 0)
                }
            for r in raw["target_preset"]:
                if r['タイプ'] == 'presets': self.presets = json.loads(r['データ'])
                elif r['タイプ'] == 'target': 
                    loaded = json.loads(r['データ'])
                    if "salt" not in loaded: loaded["salt"] = 7.0
                    self.target = loaded
            self.history = self.build_history_dict_from_rows(raw["history"])
            self.training_df = pd.DataFrame(raw["training"]) if raw["training"] else pd.DataFrame(
                columns=["日付", "Day", "種目", "セット", "重量(kg)", "回数"])
        except Exception as e: st.error(f"⚠️ データ同期エラー: {e}")

    def build_history_dict_from_rows(self, records):
        """食事履歴シート(1食材=1行)から表示用の辞書を組み立てる。"""
        hist = {}
        for r in records:
            d = str(r.get('日付', '')).strip()
            slot = r.get('時間帯', '')
            if not d or slot not in self.slots: continue
            if d not in hist:
                hist[d] = {
                    "meals_by_slot": {s: [] for s in self.slots},
                    "intake_by_slot": {s: {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbohydrate": 0.0, "salt": 0.0} for s in self.slots}
                }
            qty, unit, name = r.get('数量', ''), r.get('単位', ''), r.get('食材名', '')
            label = f"{name}({qty}{unit})" if qty not in ('', None) and unit not in ('', None) else str(name)
            hist[d]["meals_by_slot"][slot].append(label)
            intake = hist[d]["intake_by_slot"][slot]
            intake["calories"] += float(r.get('カロリー', 0) or 0)
            intake["protein"] += float(r.get('タンパク質', 0) or 0)
            intake["fat"] += float(r.get('脂質', 0) or 0)
            intake["carbohydrate"] += float(r.get('炭水化物', 0) or 0)
            intake["salt"] += float(r.get('塩分', 0) or 0)
        return hist

    def trigger_refresh(self): st.session_state["clear_cache"] = True

    def save_master_to_sheets(self):
        if not hasattr(self, 'sh'): return
        try:
            sheet = self.sh.worksheet("master")
            sheet.clear()
            sheet.append_row(['食材名', '単位', 'カロリー', 'タンパク質', '脂質', '炭水化物', '塩分'])
            for name, d in self.food_master.items():
                u = "100gあたり" if d["unit_type"] == "g" else "1個/1杯あたり"
                sheet.append_row([name, u, d["calories"], d["protein"], d["fat"], d["carbohydrate"], d.get("salt", 0.0)])
            self.trigger_refresh()
        except Exception: pass

    def save_target_preset_to_sheets(self):
        if not hasattr(self, 'sh'): return
        try:
            try: sheet = self.sh.worksheet("target_preset")
            except Exception: sheet = self.sh.add_worksheet(title="target_preset", rows="10", cols="2")
            sheet.clear()
            sheet.append_row(['タイプ', 'データ'])
            sheet.append_row(['target', json.dumps(self.target, ensure_ascii=False)])
            sheet.append_row(['presets', json.dumps(self.presets, ensure_ascii=False)])
            self.trigger_refresh()
        except Exception: pass

    def append_history_rows(self, rows):
        """rows: [[日付,時間帯,食材名,数量,単位,カロリー,タンパク質,脂質,炭水化物,塩分], ...]
        1回のAPI呼び出しでまとめて追記する。"""
        if not hasattr(self, 'sh'): return False
        try:
            try: sheet = self.sh.worksheet("history")
            except Exception:
                sheet = self.sh.add_worksheet(title="history", rows="2000", cols="10")
                sheet.append_row(["日付", "時間帯", "食材名", "数量", "単位", "カロリー", "タンパク質", "脂質", "炭水化物", "塩分"])
            sheet.append_rows(rows)
            self.trigger_refresh()
            return True
        except Exception as e:
            st.error(f"⚠️ 記録の保存に失敗しました: {e}")
            return False

    def migrate_history_if_needed(self):
        """旧形式(日付・データ(JSON))のhistoryシートを、日付・時間帯ごとの表形式に1回だけ自動変換する。
        旧データは時間帯ごとの合計値までしか復元できないため、食材の内訳は結合した文字列として残す。"""
        if not hasattr(self, 'sh'): return
        new_header = ["日付", "時間帯", "食材名", "数量", "単位", "カロリー", "タンパク質", "脂質", "炭水化物", "塩分"]
        try:
            sheet = self.sh.worksheet("history")
        except Exception:
            return
        try:
            values = sheet.get_all_values()
        except Exception:
            return
        if not values or values[0] == new_header: return
        new_rows = []
        for row in values[1:]:
            if len(row) < 2 or not row[1]: continue
            try: data = json.loads(row[1])
            except Exception: continue
            d_str = row[0]
            meals_by_slot = data.get("meals_by_slot", {})
            intake_by_slot = data.get("intake_by_slot", {})
            for slot in self.slots:
                meals = meals_by_slot.get(slot, [])
                if not meals: continue
                intake = intake_by_slot.get(slot, {})
                new_rows.append([d_str, slot, "、".join(meals), "", "",
                                  intake.get("calories", 0), intake.get("protein", 0),
                                  intake.get("fat", 0), intake.get("carbohydrate", 0), intake.get("salt", 0)])
        sheet.clear()
        sheet.append_row(new_header)
        if new_rows: sheet.append_rows(new_rows)

    def log_weight_fat(self, date_str, weight, fat):
        try:
            try: sheet = self.sh.worksheet("body")
            except Exception:
                sheet = self.sh.add_worksheet(title="body", rows="100", cols="3")
                sheet.append_row(['日付', '体重(kg)', '体脂肪率(%)'])
            cells = sheet.get_all_values()
            if not cells or len(cells) <= 1:
                sheet.append_row([date_str, weight, fat])
                return True
            d_col = [row[0] for row in cells]
            if date_str in d_col:
                idx = d_col.index(date_str) + 1
                sheet.update_cell(idx, 2, weight); sheet.update_cell(idx, 3, fat)
            else: sheet.append_row([date_str, weight, fat])
            return True
        except Exception: return False

    def get_body_dataframe(self):
        try:
            sheet = self.sh.worksheet("body")
            recs = sheet.get_all_records()
            if not recs: return None
            df = pd.DataFrame(recs)
            df['日付'] = pd.to_datetime(df['日付'])
            df = df.sort_values('日付').reset_index(drop=True)
            df['体重(7日平均)'] = df['体重(kg)'].rolling(window=7, min_periods=1).mean()
            df['体脂肪率(7日平均)'] = df['体脂肪率(%)'].rolling(window=7, min_periods=1).mean()
            return df
        except Exception: return None

    def init_day_if_needed(self, date_str):
        if date_str not in self.history:
            self.history[date_str] = {
                "meals_by_slot": {s: [] for s in self.slots},
                "intake_by_slot": {s: {"calories":0.0,"protein":0.0,"fat":0.0,"carbohydrate":0.0,"salt":0.0} for s in self.slots}
            }
        else:
            for s in self.slots:
                if "salt" not in self.history[date_str]["intake_by_slot"][s]:
                    self.history[date_str]["intake_by_slot"][s]["salt"] = 0.0

    def register_master(self, n, ut, cal, p, f, c, s):
        self.food_master[n] = {"unit_type":ut,"calories":cal,"protein":p,"fat":f,"carbohydrate":c,"salt":s}
        self.save_master_to_sheets()

    def register_preset(self, n, items):
        self.presets[n] = items
        self.save_target_preset_to_sheets()

    def log_single_item(self, d_str, slot, name, amount):
        if name not in self.food_master: return
        m = self.food_master[name]
        f = (amount / 100.0) if m["unit_type"] == "g" else amount
        unit_label = "g" if m["unit_type"] == "g" else "個"
        row = [d_str, slot, name, amount, unit_label,
               round(m["calories"] * f, 1), round(m["protein"] * f, 1),
               round(m["fat"] * f, 1), round(m["carbohydrate"] * f, 1), round(m.get("salt", 0.0) * f, 2)]
        self.append_history_rows([row])

    def log_preset_meal(self, d_str, slot, p_name):
        if p_name not in self.presets: return
        rows = []
        for item in self.presets[p_name]:
            name, amount = item["name"], item["amount"]
            if name not in self.food_master: continue
            m = self.food_master[name]
            f = (amount / 100.0) if m["unit_type"] == "g" else amount
            unit_label = "g" if m["unit_type"] == "g" else "個"
            rows.append([d_str, slot, name, amount, unit_label,
                         round(m["calories"] * f, 1), round(m["protein"] * f, 1),
                         round(m["fat"] * f, 1), round(m["carbohydrate"] * f, 1), round(m.get("salt", 0.0) * f, 2)])
        if rows: self.append_history_rows(rows)

    def get_recent_foods(self, limit=10):
        cts = {}
        for d in self.history.values():
            for ml in d["meals_by_slot"].values():
                for m in ml:
                    rn = m.split("(")[0]
                    if rn in self.food_master: cts[rn] = cts.get(rn, 0) + 1
        return [f[0] for f in sorted(cts.items(), key=lambda x:x[1], reverse=True)[:limit]]

    def log_training(self, date_str, day_type, entries):
        """entries: [{"exercise":..., "set_no":..., "weight":..., "reps":...}, ...]
        1日分をまとめて1回のAPI呼び出し(append_rows)で追記する。"""
        if not hasattr(self, 'sh'): return False
        try:
            try:
                sheet = self.sh.worksheet("training")
            except Exception:
                sheet = self.sh.add_worksheet(title="training", rows="1000", cols="6")
                sheet.append_row(["日付", "Day", "種目", "セット", "重量(kg)", "回数"])
            rows = [[date_str, day_type, e["exercise"], e["set_no"], e["weight"], e["reps"]] for e in entries]
            sheet.append_rows(rows)
            self.trigger_refresh()
            return True
        except Exception as e:
            st.error(f"⚠️ トレーニング記録の保存に失敗しました: {e}")
            return False

    def get_training_dataframe(self):
        df = self.training_df
        if df is None or df.empty: return None
        df = df.copy()
        df['日付'] = pd.to_datetime(df['日付'])
        for col in ['セット', '重量(kg)', '回数']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        # 総負荷量: 重量を扱う種目は 重量×回数、回数のみの種目(腹筋)は回数そのものを負荷量とする
        df['総負荷量'] = df.apply(
            lambda r: r['回数'] if r['種目'] in REPS_ONLY_EXERCISES else r['重量(kg)'] * r['回数'], axis=1)
        return df.sort_values('日付').reset_index(drop=True)

    def get_weekly_muscle_load(self, df):
        """部位別・週単位の総負荷量を集計して返す(週は月曜始まり)。"""
        if df is None or df.empty: return None
        week_start = df['日付'] - pd.to_timedelta(df['日付'].dt.weekday, unit='D')
        recs = []
        for (wk, load, ex) in zip(week_start, df['総負荷量'], df['種目']):
            for muscle, ratio in MUSCLE_MAP.get(ex, {}).items():
                recs.append({"週": wk, "部位": muscle, "負荷量": load * ratio})
        if not recs: return None
        m = pd.DataFrame(recs).groupby(["週", "部位"], as_index=False)["負荷量"].sum()
        return m

app = UltimateSheetsDietAppV4()

# ----------------------------------------------------
# UI
# ----------------------------------------------------
st.set_page_config(page_title="AI食事＆身体管理システム", layout="wide")
st.title("💪 AI食事管理 ＆ 体組成アナリティクス")

sel_date = st.date_input("日付", datetime.today()).strftime("%Y-%m-%d")
app.init_day_if_needed(sel_date)
day_data = app.history.get(sel_date)

total = {"calories":0.0,"protein":0.0,"fat":0.0,"carbohydrate":0.0,"salt":0.0}
for s in app.slots:
    for k in total: total[k] += day_data["intake_by_slot"][s].get(k, 0.0)

tab_main, tab_training, tab_graph = st.tabs(["📝 今日の記録・食事明細", "🏋️ トレーニング記録", "📈 トレンドグラフ"])

with tab_main:
    col1, col2 = st.columns([3, 2])
    with col1:
        st.subheader(f"📊 {sel_date} の充足度（時間帯別）")
        metrics = [("🔥 エネルギー (kcal)","calories","kcal"), ("🥩 たんぱく質 (g)","protein","g"), ("🥑 脂質 (g)","fat","g"), ("🍚 炭水化物 (g)","carbohydrate","g"), ("🧂 塩分 (g)","salt","g")]

        for label, key, unit in metrics:
            cur, tar = total[key], app.target.get(key, 7.0)
            if key == "salt":
                diff = tar - cur
                dt = f"残り: {diff:.1f}{unit}" if diff >= 0 else f"⚠️ 超過: {abs(diff):.1f}{unit}"
                st.write(f"**{label}** : {cur:.2f} / {tar:.1f} {unit} ({dt})")
            else:
                st.write(f"**{label}** : {cur:.1f} / {tar} {unit} (残り: {(tar-cur):.1f} {unit})")
            
            if cur > 0:
                slot_list = []
                for s in app.slots:
                    amt = day_data["intake_by_slot"][s].get(key, 0.0)
                    if amt > 0: slot_list.append({"時間帯":s, "割合(%)":(amt/tar)*100, "実数値":f"{amt:.1f}{unit}" if key!="salt" else f"{amt:.2f}{unit}"})
                if slot_list:
                    fig = px.bar(pd.DataFrame(slot_list), x="割合(%)", y=[label]*len(slot_list), color="時間帯", orientation="h", text="実数値", color_discrete_map={"朝食":"#2ca02c","昼食":"#1f77b4","夕食":"#ff7f0e","間食":"#d62728"})
                    fig.update_layout(xaxis=dict(range=[0, max(100, (cur/tar)*100+10)], title=None, tickfont=dict(size=10)), yaxis=dict(visible=False), showlegend=True, legend=dict(orientation="h", yanchor="top", y=-0.6, xanchor="center", x=0.5, font=dict(size=10), title=None), margin=dict(l=5, r=5, t=5, b=40), height=90)
                    fig.update_traces(textposition="inside")
                    st.plotly_chart(fig, use_container_width=True, key=f"bar_{sel_date}_{key}_v5")
                else: st.caption("記録なし")
            else: st.caption("記録なし")
            st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)

    with col2:
        st.subheader(f"⏰ {sel_date} の食事明細")
        for s in app.slots:
            meals, intake = day_data["meals_by_slot"][s], day_data["intake_by_slot"][s]
            with st.expander(f"【{s}】 {intake['calories']:.1f} kcal", expanded=True):
                if meals:
                    for m in meals: st.write(f"・ {m}")
                    st.caption(f"P:{intake['protein']:.1f} F:{intake['fat']:.1f} C:{intake['carbohydrate']:.1f} S:{intake.get('salt',0.0):.2f}")
                else: st.write("記録なし")

    st.markdown("---")
    st.subheader("入力フォーム")
    # 💡 5番目のタブに「目標設定」を追加
    sub_tab1, sub_tab2, sub_tab3, sub_tab4, sub_tab5 = st.tabs(["🏃‍♂️ 体組成を記録", "🥑 単体食材", "🍱 セットメニュー", "⚙️ マスター登録", "🎯 目標設定"])
    
    with sub_tab1:
        with st.form("body_log"):
            w = st.number_input("体重 (kg)", min_value=0.0, value=71.0, step=0.1)
            f = st.number_input("体脂肪率 (%)", min_value=0.0, value=15.0, step=0.1)
            if st.form_submit_button("記録"):
                if app.log_weight_fat(sel_date, w, f): st.success("記録完了！"); st.rerun()

    with sub_tab2:
        # 💡 入力モードを切り替えるラジオボタンを新設
        input_mode = st.radio("入力モードを選択", ["マスターから選択", "✍️ スポット入力（その場限りの外食など）"], horizontal=True, key="meal_input_mode")
        
        if input_mode == "マスターから選択":
            if app.food_master:
                recent = app.get_recent_foods(10)
                if recent:
                    st.write("🌟 よく使う食材")
                    cols = st.columns(min(len(recent), 5))
                    for i, rf in enumerate(recent):
                        with cols[i%5]:
                            if st.button(rf, key=f"rec_{rf}"): st.session_state["sel_f"] = rf
                with st.form("single_meal"):
                    slot = st.selectbox("時間帯", app.slots)
                    def_i = list(app.food_master.keys()).index(st.session_state["sel_f"]) if "sel_f" in st.session_state and st.session_state["sel_f"] in app.food_master else 0
                    f_name = st.selectbox("食材", list(app.food_master.keys()), index=def_i)
                    amt = st.number_input(f"数量 ({'g' if app.food_master[f_name]['unit_type']=='g' else '個'})", min_value=0.0, value=100.0 if app.food_master[f_name]['unit_type']=='g' else 1.0)
                    if st.form_submit_button("記録"):
                        app.log_single_item(sel_date, slot, f_name, amt)
                        st.success("完了！")
                        if "sel_f" in st.session_state: 
                            del st.session_state["sel_f"]
                        st.rerun()
            else:
                st.info("マスター未登録です。「マスター登録」タブから食材を追加するか、スポット入力をご利用ください。")
                
        else:
            # 💡 スポット入力専用のフォーム
            st.write("📝 **メニュー名と栄養素を直接入力して、今日の記録に加えます**")
            with st.form("spot_meal_form"):
                spot_slot = st.selectbox("時間帯", app.slots, key="spot_slot")
                spot_name = st.text_input("メニュー・商品名（例: カツカレー、牛丼大盛）", value="外食メニュー")
                
                # 栄養素を横並びで入力しやすく配置
                sc1, sc2, sc3, sc4, sc5 = st.columns(5)
                with sc1: spot_cal = st.number_input("カロリー (kcal)", min_value=0.0, step=10.0, value=0.0)
                with sc2: spot_p = st.number_input("P タンパク質 (g)", min_value=0.0, step=1.0, value=0.0)
                with sc3: spot_f = st.number_input("F 脂質 (g)", min_value=0.0, step=1.0, value=0.0)
                with sc4: spot_c = st.number_input("C 炭水化物 (g)", min_value=0.0, step=1.0, value=0.0)
                with sc5: spot_s = st.number_input("塩分 (g)", min_value=0.0, step=0.1, value=0.0)
                
                if st.form_submit_button("スポット食事を記録する", type="primary"):
                    if spot_name:
                        row = [sel_date, spot_slot, spot_name, "", "", spot_cal, spot_p, spot_f, spot_c, spot_s]
                        if app.append_history_rows([row]):
                            st.success(f"✅ 『{spot_name}』をスポット記録しました！")
                            st.rerun()
                    else:
                        st.error("メニュー名を入力してください。")

    with sub_tab3:
        if app.presets:
            with st.form("preset_meal"):
                slot = st.selectbox("時間帯", app.slots, key="ps")
                pn = st.selectbox("セット名", list(app.presets.keys()))
                if st.form_submit_button("一括記録"):
                    app.log_preset_meal(sel_date, slot, pn); st.success("完了！"); st.rerun()
        else: st.info("セット未登録")

    with sub_tab4:
        with st.form("m_reg"):
            n = st.text_input("食材名")
            ut = st.radio("単位", ["100gあたり", "1個/1杯あたり"], horizontal=True)
            c, p, f, car, s = st.columns(5)
            with c: m_c = st.number_input("kcal")
            with p: m_p = st.number_input("P(g)")
            with f: m_f = st.number_input("F(g)")
            with car: m_car = st.number_input("C(g)")
            with s: m_s = st.number_input("塩分(g)")
            if st.form_submit_button("マスター登録"):
                if n: app.register_master(n, "g" if ut=="100gあたり" else "count", m_c, m_p, m_f, m_car, m_s); st.success(f"登録：{n}"); st.rerun()

        st.markdown("---")
        with st.form("preset_reg"):
            pn = st.text_input("新規セットメニュー名")
            sel_items = []
            if app.food_master:
                sq = st.text_input("🔍 食材検索")
                filtered = [nm for nm in app.food_master.keys() if sq.lower() in nm.lower()]
                for nm in filtered:
                    c1, c2 = st.columns([1,1])
                    with c1: check = st.checkbox(nm, key=f"p_c_{nm}")
                    with c2: amt = st.number_input(f"{nm}量", min_value=0.0, value=100.0 if app.food_master[nm]["unit_type"]=="g" else 1.0, key=f"p_a_{nm}", label_visibility="collapsed")
                    if check: sel_items.append({"name":nm, "amount":amt})
            if st.form_submit_button("セット登録"):
                if pn and sel_items: app.register_preset(pn, sel_items); st.success(f"登録：{pn}"); st.rerun()

    # 💡 5番目のタブ：目標設定
    with sub_tab5:
        st.write("🏃‍♂️ **1日の目標・制限値を設定します**")
        with st.form("target_form"):
            t_cal = st.number_input("目標カロリー (kcal)", value=app.target["calories"], step=50)
            t_p = st.number_input("目標タンパク質 (g)", value=app.target["protein"], step=5)
            t_f = st.number_input("目標脂質 (g)", value=app.target["fat"], step=5)
            t_c = st.number_input("目標炭水化物 (g)", value=app.target["carbohydrate"], step=5)
            t_s = st.number_input("制限塩分 (g)", value=app.target.get("salt", 7.0), step=0.1)
            if st.form_submit_button("目標数値を保存して更新", type="primary"):
                app.target = {"calories": t_cal, "protein": t_p, "fat": t_f, "carbohydrate": t_c, "salt": t_s}
                app.save_target_preset_to_sheets()
                st.success("✅ クラウドの目標値を更新しました！")
                st.rerun()

with tab_training:
    st.subheader(f"🏋️ {sel_date} のトレーニング記録")
    day_type = st.radio("今日のメニュー", ["A", "B"], horizontal=True,
                         format_func=lambda d: f"Day {d}（" + "・".join(n for n, _ in TRAINING_PLAN[d]) + "）",
                         key="train_day_type")

    with st.form("training_log"):
        inputs = {}
        for ex_name, n_sets in TRAINING_PLAN[day_type]:
            st.markdown(f"**{ex_name}**" + ("(回数のみ)" if ex_name in REPS_ONLY_EXERCISES else ""))
            cols = st.columns(n_sets)
            for i in range(n_sets):
                with cols[i]:
                    if ex_name in REPS_ONLY_EXERCISES:
                        w = 0.0
                        r = st.number_input(f"{i+1}セット目 回数", min_value=0, step=1,
                                             key=f"r_{day_type}_{ex_name}_{i}")
                    else:
                        w = st.number_input(f"{i+1}セット目 重量(kg)", min_value=0.0, step=5.0,
                                             key=f"w_{day_type}_{ex_name}_{i}")
                        r = st.number_input(f"{i+1}セット目 回数", min_value=0, step=1,
                                             key=f"r_{day_type}_{ex_name}_{i}")
                    inputs[(ex_name, i + 1)] = (w, r)
        if st.form_submit_button("この日のトレーニングを記録する", type="primary"):
            entries = [
                {"exercise": ex, "set_no": set_no, "weight": w, "reps": r}
                for (ex, set_no), (w, r) in inputs.items() if w > 0 or r > 0
            ]
            if entries and app.log_training(sel_date, day_type, entries):
                st.success("✅ トレーニングを記録しました！")
                st.rerun()
            elif not entries:
                st.warning("重量か回数を1つ以上入力してください。")

    st.markdown("---")
    st.subheader("📈 種目別・総負荷量の推移")
    tr_df = app.get_training_dataframe()
    if tr_df is not None and not tr_df.empty:
        ex_list = sorted(tr_df['種目'].unique())
        sel_ex = st.selectbox("種目を選択", ex_list)
        ex_df = tr_df[tr_df['種目'] == sel_ex]
        is_reps_only = sel_ex in REPS_ONLY_EXERCISES
        # 総負荷量(重量×回数、腹筋は回数)を日付ごとに合計 → 漸進的過負荷の確認用
        daily_load = ex_df.groupby('日付')['総負荷量'].sum().reset_index()
        y_label = "総負荷量(回数)" if is_reps_only else "総負荷量(kg×回)"
        fig = px.line(daily_load, x='日付', y='総負荷量', markers=True, labels={"総負荷量": y_label})
        fig.update_layout(margin=dict(l=10, r=10, t=5, b=5), height=300)
        st.plotly_chart(fig, use_container_width=True, key=f"train_trend_{sel_ex}")
        with st.expander("記録の詳細を見る"):
            st.dataframe(ex_df.sort_values('日付', ascending=False), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.subheader("📊 部位別・週単位の総負荷量")
        st.caption("種目の総負荷量を、主働筋1.0・補助筋0.5の比率で各部位に按分し、週(月曜始まり)単位で合計しています。腹筋は回数ベースのため他部位と単位が異なる点にご注意ください。")
        weekly = app.get_weekly_muscle_load(tr_df)
        if weekly is not None:
            weekly_disp = weekly.copy()
            weekly_disp['週'] = weekly_disp['週'].dt.strftime('%Y-%m-%d') + "の週"
            fig2 = px.line(weekly_disp, x='週', y='負荷量', color='部位', markers=True)
            fig2.update_layout(margin=dict(l=10, r=10, t=5, b=5), height=350)
            st.plotly_chart(fig2, use_container_width=True, key="weekly_muscle_load")
            with st.expander("週別・部位別の数値を見る"):
                pivot = weekly_disp.pivot(index='週', columns='部位', values='負荷量').fillna(0).round(1)
                st.dataframe(pivot, use_container_width=True)
    else:
        st.info("まだトレーニング記録がありません。上のフォームから記録してください。")

with tab_graph:
    body_df = app.get_body_dataframe()
    if body_df is not None and not body_df.empty:
        cdf = body_df.set_index('日付')
        r1_1, r1_2 = st.columns(2); r2_1, r2_2 = st.columns(2)
        def draw_lc(df, col, clr):
            mi = df[col].min() - (df[col].max()-df[col].min())*0.1 - 0.2
            ma = df[col].max() + (df[col].max()-df[col].min())*0.1 + 0.2
            fig = px.line(df.reset_index(), x='日付', y=col, render_mode='svg')
            fig.update_traces(line_color=clr, mode='lines+markers')
            fig.update_layout(yaxis=dict(range=[mi, ma], title=col), xaxis=dict(title=None, tickfont=dict(size=10)), margin=dict(l=10, r=10, t=5, b=5), height=180)
            st.plotly_chart(fig, use_container_width=True, key=f"trend_{col}")
        with r1_1: st.write("📉 体重 (Daily)"); draw_lc(cdf, '体重(kg)', '#1f77b4')
        with r1_2: st.write("🌊 体重 (7日平均)"); draw_lc(cdf, '体重(7日平均)', '#aec7e8')
        with r2_1: st.write("🥑 体脂肪 (Daily)"); draw_lc(cdf, '体脂肪率(%)', '#ff7f0e')
        with r2_2: st.write("🌿 体脂肪 (7日平均)"); draw_lc(cdf, '体脂肪率(7日平均)', '#ffbb78')
    else: st.info("データなし")