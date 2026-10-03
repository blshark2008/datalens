import os
import io
import tempfile
from datetime import date as typedate
from typing import Dict, Any, List

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ============================================================
# ЗАГЛУШКИ для функций, которые не вошли в класс
# ============================================================

VMP_SURGERY_MAP: Dict[int, Any] = {}

def anonymize_name(full_name: str) -> str:
    if not full_name or pd.isna(full_name):
        return None
    parts = str(full_name).split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return str(full_name)

def get_surgery_short_name(year, items_list, vmp_num_group, hosp_list_opers, surgery_map):
    if not items_list:
        return None
    return str(items_list)[:200]


# ============================================================
# КЛАСС ЗАГРУЗЧИКА РЕЕСТРА
# ============================================================

class ReestrEventLoader:
    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config
        self.log: List[str] = []  # собираем сообщения для пользователя

    def _log(self, msg: str):
        """Пишет в stdout (docker logs) и собирает для ответа API."""
        print(msg)
        self.log.append(msg)

    def _fix_subj_live_code(self):
        conn = None
        cursor = None
        try:
            conn = psycopg2.connect(**self.db_config)
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                UPDATE patient_case
                SET subj_live_code = 'Кемеровская область - Кузбасс'
                WHERE subj_live_code IS NOT NULL
                  AND subj_live_code LIKE '%Кемеровская область - Кузбасс%';
            """)
            updated_count = cursor.rowcount
            conn.commit()
            self._log(f"✅ Обновлено {updated_count} записей: subj_live_code")
        except Exception as e:
            self._log(f"❌ Ошибка при приведении subj_live_code: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                if cursor:
                    cursor.close()
                conn.close()

    def get_department_id(self, cursor, name_of_bed_profile: str, division: str = ''):
        if (
            name_of_bed_profile is None or
            pd.isna(name_of_bed_profile) or
            str(name_of_bed_profile).strip() == '' or
            str(name_of_bed_profile).lower() == 'nan'
        ):
            return None
        division_clean = str(division).strip() if division is not None else ''
        cursor.execute(
            """
            INSERT INTO department (name_of_bed_profile, division)
            VALUES (%s, %s)
            ON CONFLICT (name_of_bed_profile, division) DO NOTHING RETURNING id;
            """,
            (name_of_bed_profile, division_clean)
        )
        result = cursor.fetchone()
        if result:
            return result['id']
        cursor.execute(
            "SELECT id FROM department WHERE name_of_bed_profile = %s AND division = %s;",
            (name_of_bed_profile, division_clean)
        )
        row = cursor.fetchone()
        if row:
            return row['id']
        raise ValueError(f"Department not found: {name_of_bed_profile} / {division_clean}")

    def get_or_create_source(self, cursor, source_name: str):
        if (
            source_name is None or
            pd.isna(source_name) or
            str(source_name).strip() == '' or
            str(source_name).lower() == 'nan'
        ):
            return None
        cursor.execute(
            "INSERT INTO source (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
            (source_name,)
        )
        result = cursor.fetchone()
        if result:
            return result['id']
        cursor.execute("SELECT id FROM source WHERE name = %s", (source_name,))
        return cursor.fetchone()['id']

    def _make_number_year(self, row):
        mrn = row['medical_record_number']
        discharge = row['discharge_date']
        if pd.isna(mrn) or not mrn or str(mrn).strip().lower() in ('', 'nan'):
            return None
        if pd.isna(discharge) or not discharge:
            return None
        return f"{str(mrn).strip()}/{discharge.year}"

    def load_reestr_file(self, file_path: str, sheet_name: str = "Данные"):
        conn = None
        cursor = None
        try:
            conn = psycopg2.connect(**self.db_config)
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            self._log(f"✅ Подключено к БД для загрузки реестра: {file_path}")
            self._log(f"📋 Чтение файла, лист: «{sheet_name}»")

            df = pd.read_excel(
                file_path,
                sheet_name=sheet_name,
                skiprows=3,
                header=0
            )
            df.dropna(how='all', inplace=True)

            self._log(f"📋 Исходные колонки ({len(df.columns)}):")
            for i, col in enumerate(df.columns):
                self._log(f"   [{i}] {repr(col)}")
            self._log(f"📋 Первые 10 строк (исходные):")
            self._log(df.head(10).to_string(max_cols=10, max_colwidth=30))

            column_mapping = {
                'RN~000': 'rn',
                'TER_STR~000': 'ter_str',
                'SUBJ_STR~000': 'subj_str',
                'ADR_LIVE_CODE~000': 'adr_live_code',
                'SUBJ_LIVE_CODE~000': 'subj_live_code',
                'PAT_ADR_LIVE~000': 'pat_adr_live',
                'VL_DAT1~000': 'vl_dat1',
                'P_FIO~000': 'full_name',
                'P_SEX~000': 'p_sex',
                'P_BD~000': 'p_bd',
                'P_AGE~000': 'p_age',
                'P_SOC~000': 'p_soc',
                'P_PRIVS~000': 'p_privs',
                'P_NUM~000':'medical_record_number',
                'DIAG_OSN~000': 'diag_osn',
                'VR_TYP_OBR~000': 'vr_typ_obr',
                'TYP_HELP_CODE~000': 'typ_help_code',
                'TYP_HELP_TEXT~000': 'typ_help_text',
                'FORM_HOSP~000': 'form_hosp',
                'TYP_STAC_HOSP~000': 'typ_stac_hosp',
                'PROF_HOSP~000': 'prof_hosp',
                'VL_DEPPROF_TEXT~000': 'vl_depprof_text',
                'DEPPROF_CODE~000': 'depprof_code',
                'PROF_BED~000': 'prof_bed',
                'DAT_HIR_LECH~000': 'dat_hir_lech',
                'VR_DAT~000': 'vr_dat',
                'VL_DAT1_FIN~000': 'discharge_date',
                'DIAG_OSN_HOSP~000': 'diag_osn_hosp',
                'RESULT_HOSP~000': 'result_hosp',
                'ISH_HOSP~000': 'ish_hosp',
                'CNT_DAYS_BEFORE_HIR~000': 'cnt_days_before_hir',
                'CNT_DAYS_AFTER_HIR~000': 'cnt_days_after_hir',
                'FINANCE_HOSP~000': 'finance_hosp',
                'OPER_VMP~000': 'oper_vmp',
                'HOSP_LIST_OPERS~000': 'hosp_list_opers',
                'OPERS1~000': 'opers1',
                'NAME_OPERS1~000': 'name_opers1',
                'OPERS_2~000': 'opers_2',
                'NAME_OPERS_2~000': 'name_opers_2',
                'EXST_OSL_AFTER_OPER~000': 'exst_osl_after_oper',
                'VMP_NUM_VIS~000': 'vmp_num_vis',
                'VMP_NUM_GROUP~000': 'vmp_num_group',
                'VMP_TYP_VMP~000': 'vmp_typ_vmp',
                'VMP_TEXT_METH~000': 'vmp_text_meth',
                'MP_NUM_GROUP~000': 'mp_num_group',
                'MP_GROUP_KSG~000': 'mp_group_ksg',
                'MP_TEXT_SERVS~000': 'mp_text_servs',
                'Шифр госпитализации~000': 'shifr_gosp',

                
                
                # Обязательные поля
                '№ ИБ': 'medical_record_number',
                'ФИО': 'full_name',
                'Отделение': 'department_name',
                'Дата выписки': 'discharge_date',
                'КСГ': 'ksg_code',
                'ВМП': 'vmp',
                'Условия оказания помощи': 'terms_of_assistance'
            }

            df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns}, inplace=True)

            if 'finance_hosp' in df.columns and 'shifr_gosp' in df.columns:
                df['source'] = df['finance_hosp'].astype(str) + ' (' + df['shifr_gosp'].astype(str) + ')'
            else:
                df['source'] = 'Неизвестно'

            for col in ['discharge_date', 'vl_dat1', 'p_bd', 'dat_hir_lech', 'vr_dat']:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], dayfirst=True, errors='coerce').dt.date

            for col in ['rn', 'ter_str', 'adr_live_code', 'typ_help_code', 'depprof_code',
                        'p_age', 'cnt_days_before_hir', 'cnt_days_after_hir', 'vmp_num_group']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype('Int64')

            string_cols = [c for c in column_mapping.values()
                           if c not in ['p_bd', 'discharge_date', 'vl_dat1', 'dat_hir_lech', 'vr_dat']]
            for col in string_cols:
                if col in df.columns:
                    df[col] = df[col].astype(str).where(df[col].notna(), None)

            if 'ter_str' in df.columns:
                df['ter_str'] = pd.to_numeric(df['ter_str'], errors='coerce').astype('Int64')
            if 'adr_live_code' in df.columns:
                df['adr_live_code'] = pd.to_numeric(df['adr_live_code'], errors='coerce').astype('Int64')

            self._log(f"📊 Найдено строк в файле!: {len(df)}")
            
            for i, col in enumerate(df.columns):
                self._log(f"   [{i}] {repr(col)}")
            
            self._log(f"📊 df: {df}")

            inserted_count = 0
            updated_count = 0
            skipped_count = 0
            vmp_warnings = 0

            for idx, row in df.iterrows():
                # self._log(f"📊 Строка: {idx}")
                medical_record_number = row.get('medical_record_number')
                discharge_date = row.get('discharge_date')
                ter_str = row.get('ter_str')
                adr_live_code = row.get('adr_live_code')
                hosp_list_opers = row.get('hosp_list_opers')
                p_bd = row.get('p_bd')

                date_oper = row.get('dat_hir_lech')
                date_start_lech = row.get('vr_dat')
                date_report = date_oper if pd.notna(date_oper) else date_start_lech
                
                # === Гарантированно получаем год ===
                hosp_year = date_report.year if pd.notna(date_report) else None


                # Приводим к date, если строки
                if isinstance(date_report, str):
                    date_report = pd.to_datetime(date_report, errors='coerce').date()
                if isinstance(p_bd, str):
                    p_bd = pd.to_datetime(p_bd, errors='coerce').date()


                # Вычисляем p_age
                if pd.notna(date_report) and pd.notna(p_bd):
                    age_years = date_report.year - p_bd.year
                    if (date_report.month, date_report.day) < (p_bd.month, p_bd.day):
                        age_years -= 1
                    p_age = age_years
                else:
                    p_age = None

                inobl = (
                    0 if ter_str == 32000
                    else (0 if ter_str == 0 and adr_live_code == 42 else 1)
                    if ter_str is not None and not pd.isna(ter_str)
                    else None
                )

                if not medical_record_number or pd.isna(medical_record_number):
                    skipped_count += 1
                    self._log(f"⚠️ Пропущена строка: отсутствует запись номера истории болезни")
                    continue
                if not discharge_date:
                    skipped_count += 1
                    self._log(f"⚠️ Пропущена строка: отсутствует discharge_date для ИБ={medical_record_number}")
                    continue

                discharge_year = discharge_date.year

                cursor.execute("""
                    SELECT id, vr_typ_obr
                    FROM patient_case
                    WHERE medical_record_number = %s
                      AND EXTRACT(YEAR FROM discharge_date) = %s;
                """, (medical_record_number, discharge_year))

                existing = cursor.fetchone()
                self._log(f"запись : {medical_record_number}, год={discharge_year} {existing}")
                # Получаем ID справочников
                try:
                    department_id = self.get_department_id(cursor, row.get('vl_depprof_text', ''))
                    source_id = self.get_or_create_source(cursor, row.get('source', ''))
                    number_year = self._make_number_year(row)
                except Exception as e:
                    self._log(f"⚠️ Ошибка получения ID (строка {idx}): {e}")
                    skipped_count += 1
                    continue

                # === Получаем vmp_num_group и vmp_typ_vmp ===
                vmp_num_group = row.get('vmp_num_group')
                vmp_typ_vmp = row.get('vmp_typ_vmp')
                vmp_text_meth = str(row.get('vmp_text_meth') or '').strip().lower()

                items_list = None
                items_list_VMP = None
                list_vmp = None
                list_vmp_64 = None

                if vmp_num_group is not None and not pd.isna(vmp_num_group) and float(vmp_num_group) != 0:
                # if vmp_num_group=='0' or vmp_num_group is not None and not pd.isna(vmp_num_group) and str(vmp_num_group).strip() not in ('', 'nan', 'None', '<NA>'):
                    # Приводим vmp_num_group к int, если это возможно
                    try:
                        vmp_num_group_int = int(vmp_num_group)
                        vmp_typ_vmp_str = str(vmp_typ_vmp).strip() if vmp_typ_vmp and str(vmp_typ_vmp).lower() not in ('nan', 'none', '') else ''

                        vr_dat = row.get('vr_dat')
                        if pd.isna(vr_dat) or not vr_dat:
                            p_year = typedate.today().year
                        else:
                            try:
                                p_year = pd.to_datetime(vr_dat).year
                            except Exception:
                                p_year = typedate.today().year
                        
                        
                        if vmp_typ_vmp_str:
                            result = None
                            years_to_try = [p_year, p_year + 1]

                            for year in years_to_try:
                                cursor.execute("""
                                    SELECT items_list
                                    FROM directory_vmp
                                    WHERE vmp_num_group = %s
                                    AND vmp_name = %s 
                                    AND year = %s
                                    LIMIT 1;
                                """, (vmp_num_group_int, vmp_typ_vmp_str, year))
                                result = cursor.fetchone()
                                if result and result.get('items_list'):
                                    break  # Нашли — выходим из цикла

                            if result and result.get('items_list'):
                                items_list = result['items_list']
                                finance_hosp = str(row.get('finance_hosp') or '').strip()
                                items_list_VMP = f"{finance_hosp} {items_list}" if finance_hosp else items_list
                            else:
                                
                                # пробуем поискать по методу лечения
                                result = None
                                years_to_try = [p_year, p_year + 1]

                                for year in years_to_try:
                                    cursor.execute("""
                                        SELECT items_list
                                        FROM directory_vmp
                                        WHERE vmp_num_group = %s
                                        AND treatment_method = %s 
                                        AND year = %s
                                        LIMIT 1;
                                    """, (vmp_num_group_int, vmp_text_meth, year))
                                    result = cursor.fetchone()
                                    if result and result.get('items_list'):
                                        break  # Нашли — выходим из цикла

                                if result and result.get('items_list'):
                                    items_list = result['items_list']
                                    finance_hosp = str(row.get('finance_hosp') or '').strip()
                                    items_list_VMP = f"{finance_hosp} {items_list}" if finance_hosp else items_list                                
                                
                                
                                else:
                                
                                
                                    items_list = None
                                    items_list_VMP = None
                                    print(f"⚠️ Не найдено совпадение для vmp_num_group={vmp_num_group_int}, vmp_name={vmp_typ_vmp_str}, year={p_year} и year+1={p_year + 1}")
                                    
                            list_vmp = get_surgery_short_name(p_year, items_list, vmp_num_group, None, VMP_SURGERY_MAP)
                            if int(vmp_num_group) == 64:
                                list_vmp_64 = get_surgery_short_name(p_year, items_list, vmp_num_group, hosp_list_opers, VMP_SURGERY_MAP)
                                
                                    
                    except (ValueError, TypeError):
                        # vmp_num_group не является числом — пропускаем
                        self._log(f"⚠️ vmp_num_group не может быть приведено к int: {vmp_num_group}")
                        vmp_num_group = None
                # Иначе — оставляем как None
                else:
                    finance_hosp = str(row.get('finance_hosp') or '').strip()
                    items_list_VMP = f"{finance_hosp} СМП"




                data = {
                    'number_year': number_year,
                    'department_id': department_id,
                    'full_name': anonymize_name(row['full_name']),
                    'source_id': source_id,
                    'medical_record_number': medical_record_number,
                    'discharge_date': discharge_date,
                    'rn': row['rn'],
                    'ter_str': row['ter_str'],
                    'subj_str': row['subj_str'],
                    'adr_live_code': row['adr_live_code'],
                    'subj_live_code': row['subj_live_code'],
                    'pat_adr_live': row['pat_adr_live'],
                    'vl_dat1': row['vl_dat1'],
                    'p_sex': row['p_sex'],
                    'p_bd': row['p_bd'],
                    'p_age': p_age,
                    'p_soc': row['p_soc'],
                    'p_privs': row['p_privs'],
                    'diag_osn': row['diag_osn'],
                    'vr_typ_obr': row['vr_typ_obr'],
                    'typ_help_code': row['typ_help_code'],
                    'typ_help_text': row['typ_help_text'],
                    'form_hosp': row['form_hosp'],
                    'typ_stac_hosp': row['typ_stac_hosp'],
                    'prof_hosp': row['prof_hosp'],
                    'vl_depprof_text': row['vl_depprof_text'],
                    'depprof_code': row['depprof_code'],
                    'prof_bed': row['prof_bed'],
                    'dat_hir_lech': row['dat_hir_lech'],
                    'vr_dat': row['vr_dat'],
                    'diag_osn_hosp': row['diag_osn_hosp'],
                    'result_hosp': row['result_hosp'],
                    'ish_hosp': row['ish_hosp'],
                    'cnt_days_before_hir': row['cnt_days_before_hir'],
                    'cnt_days_after_hir': row['cnt_days_after_hir'],
                    'finance_hosp': row['finance_hosp'],
                    'oper_vmp': row['oper_vmp'],
                    'hosp_list_opers': row['hosp_list_opers'],
                    'opers1': row['opers1'],
                    'name_opers1': row['name_opers1'],
                    'opers_2': row['opers_2'],
                    'name_opers_2': row['name_opers_2'],
                    'exst_osl_after_oper': row['exst_osl_after_oper'],
                    'vmp_num_vis': row['vmp_num_vis'],
                    'vmp_num_group': row['vmp_num_group'],
                    'vmp_typ_vmp': row['vmp_typ_vmp'],
                    'vmp_text_meth': row['vmp_text_meth'],
                    'mp_num_group': row['mp_num_group'],
                    'mp_group_ksg': row['mp_group_ksg'],
                    'mp_text_servs': row['mp_text_servs'],
                    'items_list': items_list,
                    'items_list_VMP': items_list_VMP,
                    'inobl': inobl,
                    'list_vmp': list_vmp,
                    'list_vmp_64': list_vmp_64,
                    'date_report': date_report,
                    'hosp_year': hosp_year
                }
                



                # Замена NaN/None на NULL   
                for k, v in data.items():
                    if pd.isna(v) or v == 'nan' or v == '<NA>':
                        data[k] = None

                # === Логика обновления ===
                if existing:
                    if existing['vr_typ_obr'] is not None and existing['vr_typ_obr'] != '' and 1==2:
                        self._log(f"ℹ️ Запись для ИБ={medical_record_number}, год={discharge_year} уже обработана. Пропускаем.")
                        continue
                    else:
                        # Обновляем
                        update_query = """
                            UPDATE patient_case SET
                                rn = %(rn)s, ter_str = %(ter_str)s, subj_str = %(subj_str)s,
                                adr_live_code = %(adr_live_code)s, subj_live_code = %(subj_live_code)s,
                                pat_adr_live = %(pat_adr_live)s, vl_dat1 = %(vl_dat1)s, p_sex = %(p_sex)s,
                                p_bd = %(p_bd)s, p_age = %(p_age)s, p_soc = %(p_soc)s, p_privs = %(p_privs)s,
                                diag_osn = %(diag_osn)s, vr_typ_obr = %(vr_typ_obr)s,
                                typ_help_code = %(typ_help_code)s, typ_help_text = %(typ_help_text)s,
                                form_hosp = %(form_hosp)s, typ_stac_hosp = %(typ_stac_hosp)s,
                                prof_hosp = %(prof_hosp)s, vl_depprof_text = %(vl_depprof_text)s,
                                depprof_code = %(depprof_code)s, prof_bed = %(prof_bed)s,
                                dat_hir_lech = %(dat_hir_lech)s, vr_dat = %(vr_dat)s,
                                diag_osn_hosp = %(diag_osn_hosp)s, result_hosp = %(result_hosp)s,
                                ish_hosp = %(ish_hosp)s, cnt_days_before_hir = %(cnt_days_before_hir)s,
                                cnt_days_after_hir = %(cnt_days_after_hir)s, finance_hosp = %(finance_hosp)s,
                                oper_vmp = %(oper_vmp)s, hosp_list_opers = %(hosp_list_opers)s,
                                opers1 = %(opers1)s, name_opers1 = %(name_opers1)s,
                                opers_2 = %(opers_2)s, name_opers_2 = %(name_opers_2)s,
                                exst_osl_after_oper = %(exst_osl_after_oper)s, vmp_num_vis = %(vmp_num_vis)s,
                                vmp_num_group = %(vmp_num_group)s, vmp_typ_vmp = %(vmp_typ_vmp)s,
                                vmp_text_meth = %(vmp_text_meth)s, mp_num_group = %(mp_num_group)s,
                                mp_group_ksg = %(mp_group_ksg)s, mp_text_servs = %(mp_text_servs)s,
                                updated_at = NOW(), items_list = %(items_list)s, items_list_VMP = %(items_list_VMP)s,
                                inobl = %(inobl)s, list_vmp = %(list_vmp)s, list_vmp_64 = %(list_vmp_64)s,
                                date_report = %(date_report)s, hosp_year = %(hosp_year)s
                            WHERE number_year = %(number_year)s;
                        """
                        cursor.execute(update_query, {
                            **data,
                            'discharge_year': discharge_year
                        })
                        self._log(f"🔄 Обновлено: ИБ={medical_record_number}, год={discharge_year}")
                else:
                    # Вставка новой записи
                    insert_query = """
                        INSERT INTO patient_case (
                            number_year, department_id, source_id,
                            full_name, medical_record_number, discharge_date,
                            rn, ter_str, subj_str, adr_live_code, subj_live_code, pat_adr_live,
                            vl_dat1, p_sex, p_bd, p_age, p_soc, p_privs, diag_osn, vr_typ_obr,
                            typ_help_code, typ_help_text, form_hosp, typ_stac_hosp, prof_hosp,
                            vl_depprof_text, depprof_code, prof_bed, dat_hir_lech, vr_dat,
                            diag_osn_hosp, result_hosp, ish_hosp, cnt_days_before_hir, cnt_days_after_hir,
                            finance_hosp, oper_vmp, hosp_list_opers, opers1, name_opers1,
                            opers_2, name_opers_2, exst_osl_after_oper, vmp_num_vis, vmp_num_group,
                            vmp_typ_vmp, vmp_text_meth, mp_num_group, mp_group_ksg, mp_text_servs, items_list, 
                            items_list_VMP, inobl, list_vmp, list_vmp_64, date_report, hosp_year
                        ) VALUES (
                            %(number_year)s, %(department_id)s, %(source_id)s, 
                            %(full_name)s, %(medical_record_number)s, %(discharge_date)s,
                            %(rn)s, %(ter_str)s, %(subj_str)s, %(adr_live_code)s, %(subj_live_code)s, %(pat_adr_live)s,
                            %(vl_dat1)s, %(p_sex)s, %(p_bd)s, %(p_age)s, %(p_soc)s, %(p_privs)s, %(diag_osn)s, %(vr_typ_obr)s,
                            %(typ_help_code)s, %(typ_help_text)s, %(form_hosp)s, %(typ_stac_hosp)s, %(prof_hosp)s,
                            %(vl_depprof_text)s, %(depprof_code)s, %(prof_bed)s, %(dat_hir_lech)s, %(vr_dat)s,
                            %(diag_osn_hosp)s, %(result_hosp)s, %(ish_hosp)s, %(cnt_days_before_hir)s, %(cnt_days_after_hir)s,
                            %(finance_hosp)s, %(oper_vmp)s, %(hosp_list_opers)s, %(opers1)s, %(name_opers1)s,
                            %(opers_2)s, %(name_opers_2)s, %(exst_osl_after_oper)s, %(vmp_num_vis)s, %(vmp_num_group)s,
                            %(vmp_typ_vmp)s, %(vmp_text_meth)s, %(mp_num_group)s, %(mp_group_ksg)s, %(mp_text_servs)s, %(items_list)s, 
                            %(items_list_VMP)s, %(inobl)s, %(list_vmp)s, %(list_vmp_64)s, %(date_report)s, %(hosp_year)s
                        );
                    """
                    cursor.execute(insert_query, data)
                    self._log(f"➕ Добавлено: ИБ={medical_record_number}, год={discharge_year}")



            conn.commit()
            self._log("✅ Загрузка реестра завершена.")
            self._log(f"📈 Итог: добавлено {inserted_count}, обновлено {updated_count}, пропущено {skipped_count}")
            if vmp_warnings:
                self._log(f"⚠️ Предупреждений ВМП: {vmp_warnings}")

            self._log("🔄 Приведение subj_live_code к каноническому виду...")
            self._fix_subj_live_code()

            return {
                "total": len(df),
                "inserted": inserted_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "log": self.log
            }


        except Exception as e:
            self._log(f"❌ Ошибка при загрузке {file_path}: {e}")
            if conn:
                conn.rollback()
            return {
                "total": 0,
                "inserted": 0,
                "updated": 0,
                "skipped": 0,
                "log": self.log
            }
            
        finally:
            if conn:
                if cursor:
                    cursor.close()
                conn.close()
            return {
                "log": self.log
                }

# ============================================================
# КЛАСС ЗАГРУЗЧИКА ЗАТРАТ
# ============================================================

class CostDataLoader:
    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config
        self.log: List[str] = []  # собираем сообщения для пользователя

    def _log(self, msg: str):
        """Пишет в stdout (docker logs) и собирает для ответа API."""
        print(msg)
        self.log.append(msg)



    def get_department_id(self, cursor, name_of_bed_profile: str, division: str = '') -> int:
        """
        Получает id отделения по имени и подразделению. Если нет — создаёт.
        Приводит division к строке для совместимости с текстовым полем в БД.
        """
        if name_of_bed_profile is None:
            return None
        
        division_clean = str(division).strip() if division is not None else ''

        cursor.execute(
            """
            INSERT INTO department (name_of_bed_profile, division)
            VALUES (%s, %s)
            ON CONFLICT (name_of_bed_profile, division) DO NOTHING
            RETURNING id;
            """,
            (name_of_bed_profile, division_clean)
        )
        result = cursor.fetchone()
        if result:
            return result['id']

        cursor.execute(
            """
            SELECT id FROM department
            WHERE name_of_bed_profile = %s AND division = %s;
            """,
            (name_of_bed_profile, division_clean)
        )
        row = cursor.fetchone()
        if row:
            return row['id']
        else:
            raise ValueError(f"Department not found: name='{name_of_bed_profile}', division='{division_clean}'")



    def get_or_create_source(self, cursor, source_name: str) -> int:
        """Получает или создаёт источник."""
        cursor.execute("INSERT INTO source (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id", (source_name,))
        result = cursor.fetchone()
        if result:
            return result['id']
        cursor.execute("SELECT id FROM source WHERE name = %s", (source_name,))
        return cursor.fetchone()['id']

    def get_or_create_terms_of_assistance(self, cursor, term: str) -> int:
        """Получает или создаёт условие оказания помощи."""
        cursor.execute("INSERT INTO terms_of_assistance (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id", (term,))
        result = cursor.fetchone()
        if result:
            return result['id']
        cursor.execute("SELECT id FROM terms_of_assistance WHERE name = %s", (term,))
        return cursor.fetchone()['id']

    
    def _make_number_year(self, row):
        mrn = row['medical_record_number']
        discharge = row['discharge_date']

        if pd.isna(mrn) or not mrn or str(mrn).strip().lower() in ('', 'nan'):
            return None
        if pd.isna(discharge) or not discharge:
            return None
        return f"{str(mrn).strip()}/{discharge.year}"    


    def load_cost_file(self, file_path: str, sheet_name: str = "результат"):
        """Загружает файл со стоимостью лечения, начиная с 10 строки."""
        conn = None
        cursor = None
        try:
            conn = psycopg2.connect(**self.db_config)
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            self._log(f"✅ Подключено к БД для загрузки: {file_path}")

            # === Чтение Excel, начиная с 10 строки ===
            df = pd.read_excel(
                file_path,
                sheet_name=sheet_name,
                skiprows=8,  # Пропускаем 0–8 → 9-я строка (Excel 10) станет заголовком
                header=0     # Первая строка — заголовок
            )
            # self._log (df)

            # Удалить пустые строки
            df.dropna(how='all', inplace=True)

            # Переименование колонок (пример — адаптируйте под реальные названия)
            column_mapping = {
                'Unnamed: 3': 'full_name',
                'Unnamed: 4': 'ksg_code',
                'Unnamed: 5': 'vmp',
                'Unnamed: 6': 'medical_record_number',
                'Unnamed: 7': 'discharge_date',
                'Unnamed: 8': 'total_bed_days',
                'Unnamed: 9': 'aro_bed_days',
                'Unnamed: 10': 'total_cost_event',
                'Unnamed: 21': 'total_cost',
                'отделение': 'meds_ward_cost',
                'реанимация': 'meds_icu_cost',
                'Оперблок': 'meds_surgery_cost',
                'R-операционной': 'meds_postop_room_cost',
                'Прочие': 'meds_other_cost',
                'ОФД/УЗИ': 'para_clinic_usg_cost',
                'рентген': 'para_clinic_xray_cost',
                'КТ, МРТ': 'para_clinic_ct_mri_cost',
                'КДЛ': 'para_clinic_lab_cost',
                'Unnamed: 0': 'department_name',
                'Unnamed: 1': 'source_name',
                'Unnamed: 2': 'terms_of_assistance'
            }
            df = df.rename(columns=column_mapping)
            
            self._log(f"Колонки: {list(df.columns)}")

            
            self._log("Примеры значений в 'discharge_date':")
            self._log(str(df['discharge_date'].head().tolist()))

            # Приведение типов
            df['discharge_date'] = pd.to_datetime(df['discharge_date'],format='%d.%m.%Y %H:%M',  errors='coerce').dt.date
            #df['vmp'] = df['vmp'].astype(str).str.lower().isin(['да', 'true', '1', 'yes'])

            self._log("Примеры значений в 'discharge_date':")
            self._log(str(df['discharge_date'].head().tolist()))

            # Замена NaN на 0 для числовых полей
            numeric_cols = [
                'total_bed_days', 'aro_bed_days', 'total_cost', 'total_cost_event',
                'meds_ward_cost', 'meds_icu_cost', 'meds_surgery_cost',
                'meds_postop_room_cost', 'meds_other_cost',
                'para_clinic_usg_cost', 'para_clinic_xray_cost',
                'para_clinic_ct_mri_cost', 'para_clinic_lab_cost'
            ]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                

            # print (df)

            # === Загрузка в БД ===
            insert_query = """
                INSERT INTO patient_case (
                    number_year, department_id, source_id, terms_of_assistance_id,
                    full_name, ksg_code, vmp, medical_record_number, discharge_date,
                    total_bed_days, aro_bed_days, total_cost, total_cost_event,
                    meds_ward_cost, meds_icu_cost, meds_surgery_cost, meds_postop_room_cost, meds_other_cost,
                    para_clinic_usg_cost, para_clinic_xray_cost, para_clinic_ct_mri_cost, para_clinic_lab_cost
                ) VALUES (
                    %(number_year)s, %(department_id)s, %(source_id)s, %(terms_of_assistance_id)s,
                    %(full_name)s, %(ksg_code)s, %(vmp)s, %(medical_record_number)s, %(discharge_date)s,
                    %(total_bed_days)s, %(aro_bed_days)s, %(total_cost)s,  %(total_cost_event)s,
                    %(meds_ward_cost)s, %(meds_icu_cost)s, %(meds_surgery_cost)s, %(meds_postop_room_cost)s, %(meds_other_cost)s,
                    %(para_clinic_usg_cost)s, %(para_clinic_xray_cost)s, %(para_clinic_ct_mri_cost)s, %(para_clinic_lab_cost)s
                )
                ON CONFLICT (number_year) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    discharge_date = EXCLUDED.discharge_date,
                    total_cost = EXCLUDED.total_cost,
                    updated_at = NOW();
            """

            inserted_count = 0
            updated_count = 0
            skipped_count = 0
            vmp_warnings = 0
            
            for idx, row in df.iterrows():
                # Получаем ID справочников
                try:
                    department_id = self.get_department_id(cursor, row['department_name'])
                    source_id = self.get_or_create_source(cursor, row['source_name'])
                    terms_id = self.get_or_create_terms_of_assistance(cursor, row['terms_of_assistance'])
                    number_year = self._make_number_year(row)
                except Exception as e:
                    self._log(f"⚠️ Ошибка получения ID для строки: {e}")
                    vmp_warnings += 1
                    continue

                # === Проверка существования записи по number_year ===
                cursor.execute("""
                    SELECT id, terms_of_assistance_id 
                    FROM patient_case 
                    WHERE number_year = %s;
                """, (number_year,))
                existing = cursor.fetchone()

                data = {
                    'number_year': number_year,
                    'department_id': department_id,
                    'source_id': source_id,
                    'terms_of_assistance_id': terms_id,
                    'full_name': anonymize_name(row['full_name']),
                    'ksg_code': row['ksg_code'],
                    'vmp': row['vmp'],
                    'medical_record_number': row['medical_record_number'],
                    'discharge_date': row['discharge_date'],
                    'total_bed_days': row['total_bed_days'],
                    'aro_bed_days': row['aro_bed_days'],
                    'total_cost': row['total_cost'],
                    'total_cost_event': row['total_cost_event'],
                    'meds_ward_cost': row['meds_ward_cost'],
                    'meds_icu_cost': row['meds_icu_cost'],
                    'meds_surgery_cost': row['meds_surgery_cost'],
                    'meds_postop_room_cost': row['meds_postop_room_cost'],
                    'meds_other_cost': row['meds_other_cost'],
                    'para_clinic_usg_cost': row['para_clinic_usg_cost'],
                    'para_clinic_xray_cost': row['para_clinic_xray_cost'],
                    'para_clinic_ct_mri_cost': row['para_clinic_ct_mri_cost'],
                    'para_clinic_lab_cost': row['para_clinic_lab_cost'],
                }

                if existing:
                    if existing['terms_of_assistance_id'] is not None:
                        self._log(f"ℹ️ Пропущено: данные уже есть для number_year={number_year}")
                        skipped_count += 1
                        continue
                    else:
                        # Обновляем поля, где terms_of_assistance_id был NULL
                        update_query = """
                            UPDATE patient_case SET
                                terms_of_assistance_id = %(terms_of_assistance_id)s,
                                ksg_code = %(ksg_code)s,
                                vmp = %(vmp)s,
                                total_bed_days = %(total_bed_days)s,
                                aro_bed_days = %(aro_bed_days)s,
                                total_cost = %(total_cost)s,
                                total_cost_event = %(total_cost_event)s,
                                meds_ward_cost = %(meds_ward_cost)s,
                                meds_icu_cost = %(meds_icu_cost)s,
                                meds_surgery_cost = %(meds_surgery_cost)s,
                                meds_postop_room_cost = %(meds_postop_room_cost)s,
                                meds_other_cost = %(meds_other_cost)s,
                                para_clinic_usg_cost = %(para_clinic_usg_cost)s,
                                para_clinic_xray_cost = %(para_clinic_xray_cost)s,
                                para_clinic_ct_mri_cost = %(para_clinic_ct_mri_cost)s,
                                para_clinic_lab_cost = %(para_clinic_lab_cost)s,
                                updated_at = NOW()
                            WHERE number_year = %(number_year)s;
                        """
                        cursor.execute(update_query, data)
                        self._log(f"🔄 Обновлено (дозагружены данные): number_year={number_year}")
                        updated_count +=1
                else:
                    # Вставка новой записи
                    insert_query = """
                        INSERT INTO patient_case (
                            number_year, department_id, source_id, terms_of_assistance_id,
                            full_name, ksg_code, vmp, medical_record_number, discharge_date,
                            total_bed_days, aro_bed_days, total_cost, total_cost_event,
                            meds_ward_cost, meds_icu_cost, meds_surgery_cost, meds_postop_room_cost, meds_other_cost,
                            para_clinic_usg_cost, para_clinic_xray_cost, para_clinic_ct_mri_cost, para_clinic_lab_cost
                        ) VALUES (
                            %(number_year)s, %(department_id)s, %(source_id)s, %(terms_of_assistance_id)s,
                            %(full_name)s, %(ksg_code)s, %(vmp)s, %(medical_record_number)s, %(discharge_date)s,
                            %(total_bed_days)s, %(aro_bed_days)s, %(total_cost)s, %(total_cost_event)s,
                            %(meds_ward_cost)s, %(meds_icu_cost)s, %(meds_surgery_cost)s, %(meds_postop_room_cost)s, %(meds_other_cost)s,
                            %(para_clinic_usg_cost)s, %(para_clinic_xray_cost)s, %(para_clinic_ct_mri_cost)s, %(para_clinic_lab_cost)s
                        );
                    """
                    cursor.execute(insert_query, data)
                    self._log(f"➕ Добавлено: number_year={number_year}")
                    inserted_count +=1

            conn.commit()
            self._log(f"✅ Файл со стоимостью успешно загружен: {file_path}")

            return {
                "total": len(df),
                "inserted": inserted_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "log": self.log
            }
            
        except Exception as e:
            print(f"❌ Ошибка при загрузке {file_path}: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                if cursor:
                    cursor.close()
                conn.close()
            



# ============================================================
# ТИПЫ ЗАГРУЖАЕМЫХ ФАЙЛОВ
# ============================================================

FILE_TYPES = {
    "daily_sheet": {
        "label": "Лист ежедневного учета",
        "comment": "Загрузка отчетов «ЛИСТ ЕЖЕДНЕВНОГО УЧЕТА ДВИЖЕНИЯ ПАЦИЕНТОВ И КОЕЧНОГО ФОНДА МЕДИЦИНСКОЙ ОРГАНИЗАЦИИ, ОКАЗЫВАЮЩЕЙ МЕДИЦИНСКУЮ ПОМОЩЬ В СТАЦИОНАРНЫХ УСЛОВИЯХ, В УСЛОВИЯХ ДНЕВНОГО СТАЦИОНАРА за период с»"
    },
    "meds_expense": {
        "label": "Расход ЛС, МИ по пролеченным больным",
        "comment": "Загрузка отчета «Расход ЛС, МИ по пролеченным больным»"
    },
    "reestr": {
        "label": "Реестр случаев",
        "comment": "Основной отчет. Реестр случаев"
    }
}


# ============================================================
# Flask API
# ============================================================

DB_CONFIG = {
    "host": os.environ.get("PG_HOST", "postgres"),
    "port": int(os.environ.get("PG_PORT", "5432")),
    "user": os.environ.get("PG_USER", "pg-user"),
    "database": os.environ.get("PG_DB", "pg-us-db"),
    "password": os.environ.get("PG_PASSWORD", ""),
}


@app.route("/api/internal/v1/data-loader/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"message": "Файл не передан"}), 400

    file = request.files["file"]
    # sheet_name — отдельное поле, НЕ tableName
    sheet_name = request.form.get("sheet_name", "Данные")
    file_type = request.form.get("file_type", "reestr")

    if not file.filename:
        return jsonify({"message": "Имя файла пустое"}), 400

    suffix = os.path.splitext(file.filename)[1] or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        
        if file_type == "reestr":
            loader = ReestrEventLoader(DB_CONFIG)
            result = loader.load_reestr_file(tmp_path, sheet_name=sheet_name)
        elif file_type == "meds_expense":
            loader = CostDataLoader(DB_CONFIG)
            result = loader.load_cost_file(tmp_path, sheet_name=sheet_name)
        else:
            result = None
        
        return jsonify({
            "message": "Файл обработан",
            "rows": result.get("total", 0),
            "inserted": result.get("inserted", 0),
            "updated": result.get("updated", 0),
            "skipped": result.get("skipped", 0),
            "log": result.get("log", [])
        })
    except Exception as e:
        return jsonify({"message": f"Ошибка: {str(e)}", "log": []}), 500
    finally:
        os.unlink(tmp_path)


@app.route("/api/internal/v1/data-loader/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)