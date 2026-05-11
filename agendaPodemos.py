import os
import sys
import asyncio
import streamlit as st
import pandas as pd
import urllib.parse
import re
import requests
import traceback
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# Garante que o Chromium esteja instalado no ambiente
os.system("playwright install chromium")

# Correção de arquitetura para subprocessos no Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ==========================================
# CONFIGURAÇÃO DA PÁGINA
# ==========================================
st.set_page_config(
    page_title="Extrator de Agendas Parlamentares",
    page_icon="🏛️",
    layout="wide"
)

# ==========================================
# INTEGRAÇÃO COM A API (PARA LISTAGEM)
# ==========================================
@st.cache_data(ttl=3600)
def obter_base_geral_deputados():
    url = "https://dadosabertos.camara.leg.br/api/v2/deputados"
    params = {"itens": 600, "ordem": "ASC", "ordenarPor": "nome"}
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status() 
        dados = response.json().get("dados", [])
        return [{"deputado": d["nome"], "codigo": str(d["id"]), "partido": d["siglaPartido"]} for d in dados]
    except Exception:
        return []

# ==========================================
# LÓGICA DE PARSING E ORDENAÇÃO
# ==========================================
def parser_ordenacao_data(data_str):
    match_padrao = re.search(r'(\d{2})/(\d{2})/(\d{4})', data_str)
    if match_padrao:
        return int(match_padrao.group(3)), int(match_padrao.group(2)), int(match_padrao.group(1))
    
    meses = {"JANEIRO": 1, "FEVEREIRO": 2, "MARÇO": 3, "ABRIL": 4, "MAIO": 5, "JUNHO": 6,
             "JULHO": 7, "AGOSTO": 8, "SETEMBRO": 9, "OUTUBRO": 10, "NOVEMBRO": 11, "DEZEMBRO": 12}
    match_extenso = re.search(r'(\d{1,2})\s+DE\s+([A-ZÇ]+)\s+DE\s+(\d{4})', data_str.upper())
    if match_extenso:
        return int(match_extenso.group(3)), meses.get(match_extenso.group(2), 1), int(match_extenso.group(1))
    return 9999, 12, 31

def parser_ordenacao_hora(hora_str):
    if hora_str == "HORA NÃO INFORMADA": return -1, -1 
    match = re.search(r'(\d{1,2})H(\d{2})', hora_str.upper())
    return (int(match.group(1)), int(match.group(2))) if match else (99, 99)

def extrair_eventos_pagina(texto_bruto):
    linhas = [linha.strip() for linha in texto_bruto.split('\n') if linha.strip()]
    nome_extraido, eventos, capturando = None, [], False
    data_at, hora_at, buffer = "DATA NÃO INFORMADA", "HORA NÃO INFORMADA", []
    
    def salvar():
        if buffer:
            desc = "\n".join(buffer).strip()
            if desc and desc.upper() not in ["NENHUM RESULTADO", "NÃO EXISTE AGENDA", "RESULTADOS DA BUSCA"]:
                eventos.append({"data": data_at, "hora": hora_at, "descricao": desc, "hash": f"{data_at}|{hora_at}|{desc}"})
            buffer.clear()

    for i, linha in enumerate(linhas):
        l_up = linha.upper()
        if l_up == "AGENDA" and i + 1 < len(linhas) and not nome_extraido:
            nome_extraido = linhas[i+1]
        if re.search(r'\d+(ª|A)?\s+LEGISLATURA', l_up):
            salvar(); break
        if "RESULTADOS DA BUSCA" in l_up:
            capturando = True; continue
        if capturando:
            if l_up in ["NENHUM RESULTADO", "NÃO EXISTE AGENDA"]: continue
            is_dt = bool(re.match(r'^\d{2}/\d{2}/\d{4}$', l_up)) or bool(re.match(r'^(SEGUNDA|TERÇA|QUARTA|QUINTA|SEXTA|SÁBADO|DOMINGO).*?\d{4}$', l_up))
            is_hr = bool(re.match(r'^\d{1,2}H\d{2}$', l_up))
            if is_dt: salvar(); data_at = linha; hora_at = "HORA NÃO INFORMADA"
            elif is_hr: salvar(); hora_at = linha
            else: buffer.append(linha)
    salvar()
    return nome_extraido, eventos

# ==========================================
# PROCESSO DE RASPAGEM (PLAYWRIGHT)
# ==========================================
def processar_agendas(df, data_ini, data_fim, ui_status, ui_progress):
    datas_alvo = [(data_ini + timedelta(days=i)) for i in range((data_fim - data_ini).days + 1)]
    str_ini, str_fim = data_ini.strftime("%d/%m/%Y"), data_fim.strftime("%d/%m/%Y")
    d_ini_enc, d_fim_enc = urllib.parse.quote(str_ini), urllib.parse.quote(str_fim)
    consolidado = []

    with sync_playwright() as p:
        # Configurações críticas para rodar via IP/Servidor
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", 
            "--disable-setuid-sandbox", 
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"
        ])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="pt-BR", timezone_id="America/Sao_Paulo"
        )
        page = context.new_page()

        for idx, row in df.iterrows():
            nome_original, codigo = str(row["deputado"]), str(row["codigo"]).strip()
            ui_status.update(label=f"Extraindo: {nome_original} ({idx+1}/{len(df)})", state="running")
            ui_progress.progress(idx / len(df))
            
            todos_evs, nome_oficial = [], None

            # 1. Varredura Diária + 2. Período Completo (Sempre as duas)
            urls = [f"https://www.camara.leg.br/deputados/{codigo}/agenda?termo=&dataInicial__proxy={urllib.parse.quote(d.strftime('%d/%m/%Y'))}&dataInicial={urllib.parse.quote(d.strftime('%d/%m/%Y'))}&dataFinal__proxy={urllib.parse.quote(d.strftime('%d/%m/%Y'))}&dataFinal={urllib.parse.quote(d.strftime('%d/%m/%Y'))}" for d in datas_alvo]
            urls.append(f"https://www.camara.leg.br/deputados/{codigo}/agenda?termo=&dataInicial__proxy={d_ini_enc}&dataInicial={d_ini_enc}&dataFinal__proxy={d_fim_enc}&dataFinal={d_fim_enc}")

            for url in urls:
                try:
                    page.goto(url, wait_until="networkidle", timeout=45000)
                    page.wait_for_timeout(1500)
                    texto = page.evaluate("document.body.innerText")
                    n_pag, evs_pag = extrair_eventos_pagina(texto)
                    if n_pag and not nome_oficial: nome_oficial = n_pag.upper()
                    if evs_pag: todos_evs.extend(evs_pag)
                except Exception: continue

            # Anti-duplicidade e Ordenação
            vistos, unicos = set(), []
            for e in todos_evs:
                if e["hash"] not in vistos:
                    vistos.add(e["hash"]); unicos.append(e)
            unicos.sort(key=lambda x: (parser_ordenacao_data(x["data"]), parser_ordenacao_hora(x["hora"])))

            # Formatação WhatsApp
            res = f"*{nome_oficial or nome_original.upper()}*\nPeríodo: {str_ini} a {str_fim}\n\n"
            if unicos:
                blocos = []
                for u in unicos:
                    d_fmt = re.sub(r'\b(\d{2}/\d{2}/\d{4})\b', r'*\1*', u["data"])
                    if not d_fmt.startswith('*'): d_fmt = f"*{d_fmt.upper()}*"
                    item = [d_fmt]
                    if u["hora"] != "HORA NÃO INFORMADA": item.append(f"•\t{u['hora']}")
                    item.append(u["descricao"])
                    blocos.append("\n".join(item))
                res += "\n\n".join(blocos)
            else: res += "Nenhuma agenda encontrada.\n"
            consolidado.append(res + "\n" + ("="*40) + "\n\n")

        browser.close()
    return "".join(consolidado)

# ==========================================
# INTERFACE
# ==========================================
base_geral = obter_base_geral_deputados()
if "lista_deputados" not in st.session_state:
    st.session_state.lista_deputados = [d for d in base_geral if d['partido'].upper() == 'PODE']
    for d in st.session_state.lista_deputados: st.session_state[f"chk_{d['codigo']}"] = True

col_l, col_r = st.columns([1.2, 2])

with col_l:
    st.subheader("📋 Parlamentares")
    c1, c2 = st.columns(2)
    if c1.button("✅ Todos"): 
        for d in st.session_state.lista_deputados: st.session_state[f"chk_{d['codigo']}"] = True
    if c2.button("❌ Nenhum"):
        for d in st.session_state.lista_deputados: st.session_state[f"chk_{d['codigo']}"] = False
    
    with st.container(height=350):
        for d in st.session_state.lista_deputados:
            st.checkbox(f"{d['deputado']} ({d['partido']})", key=f"chk_{d['codigo']}")
    
    st.divider()
    t1, t2 = st.tabs(["👤 Buscar", "🏛️ Bancada"])
    with t1:
        sel = st.multiselect("Nome", base_geral, format_func=lambda x: f"{x['deputado']} ({x['partido']})")
        if st.button("➕ Add Selecionados"):
            exs = {x['codigo'] for x in st.session_state.lista_deputados}
            for s in sel:
                if s['codigo'] not in exs: 
                    st.session_state.lista_deputados.append(s)
                    st.session_state[f"chk_{s['codigo']}"] = True
            st.rerun()
    with t2:
        sigla = st.text_input("Sigla")
        if st.button("➕ Add Bancada"):
            novos = [d for d in base_geral if d['partido'].upper() == sigla.upper()]
            exs = {x['codigo'] for x in st.session_state.lista_deputados}
            for n in novos:
                if n['codigo'] not in exs:
                    st.session_state.lista_deputados.append(n)
                    st.session_state[f"chk_{n['codigo']}"] = True
            st.rerun()

with col_r:
    st.subheader("⚙️ Execução")
    d1, d2 = st.columns(2)
    dt_ini = d1.date_input("Início", format="DD/MM/YYYY")
    dt_fim = d2.date_input("Fim", format="DD/MM/YYYY")
    
    if st.button("🚀 Iniciar Extração", type="primary"):
        selecionados = [d for d in st.session_state.lista_deputados if st.session_state.get(f"chk_{d['codigo']}")]
        if not selecionados: st.error("Selecione alguém.")
        else:
            try:
                status = st.status("Acessando Câmara...", expanded=True)
                prog = st.progress(0)
                txt = processar_agendas(pd.DataFrame(selecionados), dt_ini, dt_fim, status, prog)
                status.update(label="Concluído!", state="complete", expanded=False)
                st.download_button("📥 Baixar TXT", txt, f"Agendas_{dt_ini}.txt")
                st.text_area("Resultado", txt, height=400)
            except Exception:
                st.error("Erro Crítico")
                st.code(traceback.format_exc())
