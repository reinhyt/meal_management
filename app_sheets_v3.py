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

class UltimateSheetsDietAppV4:
    def __init__(self):
        self.target = {"calories": 2000, "protein": 120, "fat": 50, "carbohydrate": 255, "salt": 7.0}
        self.slots = ["朝食", "昼食", "夕食", "間食"]
        self.food_master = {}
        self.presets = {}
        self.history = {}
        
        if os.path.exists(CREDENTIALS_FILE) and SPREADSHEET_ID != "ここにあなたのスプレッドシートIDを貼り付けてください":
            try:
                scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
                self.gc = gspread.authorize(creds)
                self.sh = self.gc.open_by_key(SPREADSHEET_ID)
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
            return {"master": m_data, "target_preset": tp_data, "history": h_data}

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
            self.history = {}
            for r in raw["history"]: 
                data = json.loads(r['データ'])
                if "intake_by_slot" in data:
                    for slot in self.slots:
                        if slot in data["intake_by_slot"] and "salt" not in data["intake_by_slot"][slot]:
                            data["intake_by_slot"][slot]["salt"] = 0.0
                self.history[r['日付']] = data
        except Exception as e: st.error(f"⚠️ データ同期エラー: {e}")

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

    def save_history_to_sheets(self):
        if not hasattr(self, 'sh'): return
        try:
            sheet = self.sh.worksheet("history")
            sheet.clear()
            sheet.append_row(['日付', 'データ'])
            for d_str, data in self.history.items():
                sheet.append_row([d_str, json.dumps(data, ensure_ascii=False)])
            self.trigger_refresh()
        except Exception: pass

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
        self.init_day_if_needed(d_str)
        if name in self.food_master:
            m = self.food_master[name]
            f = (amount / 100.0) if m["unit_type"] == "g" else amount
            day = self.history[d_str]
            day["intake_by_slot"][slot]["calories"] += m["calories"] * f
            day["intake_by_slot"][slot]["protein"] += m["protein"] * f
            day["intake_by_slot"][slot]["fat"] += m["fat"] * f
            day["intake_by_slot"][slot]["carbohydrate"] += m["carbohydrate"] * f
            day["intake_by_slot"][slot]["salt"] += m.get("salt", 0.0) * f
            u = f"{amount}g" if m["unit_type"] == "g" else f"{amount}個"
            day["meals_by_slot"][slot].append(f"{name}({u})")
            self.save_history_to_sheets()

    def log_preset_meal(self, d_str, slot, p_name):
        if p_name in self.presets:
            for item in self.presets[p_name]: self.log_single_item(d_str, slot, item["name"], item["amount"])

    def get_recent_foods(self, limit=10):
        cts = {}
        for d in self.history.values():
            for ml in d["meals_by_slot"].values():
                for m in ml:
                    rn = m.split("(")[0]
                    if rn in self.food_master: cts[rn] = cts.get(rn, 0) + 1
        return [f[0] for f in sorted(cts.items(), key=lambda x:x[1], reverse=True)[:limit]]

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

tab_main, tab_graph = st.tabs(["📝 今日の記録・食事明細", "📈 体重・体脂肪トレンドグラフ"])

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
                        # 💡 既存の仕組み（app.food_master）を汚さずに、今日の日付データに直接栄養素を加算する処理
                        app.init_day_if_needed(sel_date)
                        day = app.history[sel_date]
                        
                        day["intake_by_slot"][spot_slot]["calories"] += spot_cal
                        day["intake_by_slot"][spot_slot]["protein"] += spot_p
                        day["intake_by_slot"][spot_slot]["fat"] += spot_f
                        day["intake_by_slot"][spot_slot]["carbohydrate"] += spot_c
                        day["intake_by_slot"][spot_slot]["salt"] += spot_s
                        
                        # 明細には「メニュー名」をそのまま突っ込む
                        day["meals_by_slot"][spot_slot].append(f"{spot_name}")
                        app.save_history_to_sheets()
                        
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