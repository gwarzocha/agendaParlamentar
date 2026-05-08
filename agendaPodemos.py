import os
os.system("playwright install chromium")

import sys
import asyncio

# ==========================================
# CORREÇÃO DE ARQUITETURA PARA WINDOWS
# ==========================================
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st
import pandas as pd
import urllib.parse
import re
import requests
import traceback
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# ==========================================
# CONFIGURAÇÃO DA PÁGINA
# ==========================================
st.set_page_config(
    page_title="Extrator de Agendas Parlamentares",
    page_icon="🏛️",
    layout="wide"
)

# ==========================================
# INTEGRAÇÃO COM A API DA CÂMARA (CACHEADA)
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
    except Exception as e:
        st.error(f"Falha ao carregar lista da Câmara: {e}")
        return []

# ==========================================
# GERENCIAMENTO DE ESTADO (SESSION STATE)
# ==========================================
base_geral = obter_base_geral_deputados()

def adicionar_a_lista(novos_deputados):
    codigos_existentes = {d['codigo'] for d in st.session_state.lista_deputados}
    adicionados = [d for d in novos_deputados if d['codigo'] not in codigos_existentes]
    
    if adicionados:
        st.session_state.lista_deputados.extend(adicionados)
        st.session_state.lista_deputados.sort(key=lambda x: x['deputado'])
        for d in adicionados:
            st.session_state[f"chk_{d['codigo']}"] = True
    else:
        st.info("Os parlamentares selecionados já estão na sua lista.")

if "lista_deputados" not in st.session_state:
    st.session_state.lista_deputados = []
    if base_geral:
        pode_default = [d for d in base_geral if d['partido'].upper() == 'PODE']
        adicionar_a_lista(pode_default)

def alternar_todos(estado):
    for d in st.session_state.lista_deputados:
        st.session_state[f"chk_{d['codigo']}"] = estado

def adicionar_partido(sigla):
    sigla = sigla.upper().strip()
    novos = [d for d in base_geral if d['partido'].upper() == sigla]
    if not novos:
        st.warning(f"Nenhum deputado encontrado para a sigla '{sigla}'.")
        return
    adicionar_a_lista(novos)

# ==========================================
# FUNÇÕES DE LÓGICA DE DADOS E ANTI-DUPLICIDADE
# ==========================================
def gerar_lista_datas(data_inicial, data_final):
    delta = data_final - data_inicial
    return [data_inicial + timedelta(days=i) for i in range(delta.days + 1)]

def parser_ordenacao_data(data_str):
    match_padrao = re.search(r'(\d{2})/(\d{2})/(\d{4})', data_str)
    if match_padrao:
        return int(match_padrao.group(3)), int(match_padrao.group(2)), int(match_padrao.group(1))
        
    match_extenso = re.search(r'(\d{1,2})\s+DE\s+([A-ZÇ]+)\s+DE\s+(\d{4})', data_str.upper())
    if match_extenso:
        dia = int(match_extenso.group(1))
        mes_str = match_extenso.group(2)
        ano = int(match_extenso.group(3))
        
        meses = {
            "JANEIRO": 1, "FEVEREIRO": 2, "MARÇO": 3, "ABRIL": 4, "MAIO": 5, "JUNHO": 6,
            "JULHO": 7, "AGOSTO": 8, "SETEMBRO": 9, "OUTUBRO": 10, "NOVEMBRO": 11, "DEZEMBRO": 12
        }
        mes = meses.get(mes_str, 1) 
        return ano, mes, dia
        
    return 9999, 12, 31

def parser_ordenacao_hora(hora_str):
    if hora_str == "HORA NÃO INFORMADA":
        return -1, -1 
    match = re.search(r'(\d{1,2})H(\d{2})', hora_str.upper())
    if match:
        return int(match.group(1)), int(match.group(2))
    return 99, 99

def extrair_eventos_pagina(texto_bruto):
    linhas = [linha.strip() for linha in texto_bruto.split('\n') if linha.strip()]
    nome_extraido = None
    eventos = []
    capturando = False
    
    data_atual = "DATA NÃO INFORMADA"
    hora_atual = "HORA NÃO INFORMADA"
    buffer_evento = []
    
    def salvar_evento():
        if buffer_evento:
            descricao = "\n".join(buffer_evento).strip()
            if descricao and descricao.upper() not in ["NENHUM RESULTADO", "NÃO EXISTE AGENDA", "RESULTADOS DA BUSCA"]:
                eventos.append({
                    "data": data_atual,
                    "hora": hora_atual,
                    "descricao": descricao,
                    "hash": f"{data_atual}|{hora_atual}|{descricao}"
                })
            buffer_evento.clear()

    for i, linha in enumerate(linhas):
        linha_upper = linha.upper()
        
        if linha_upper == "AGENDA" and i + 1 < len(linhas) and not nome_extraido:
            nome_extraido = linhas[i+1]
            
        if re.search(r'\d+(ª|A)?\s+LEGISLATURA', linha_upper):
            salvar_evento()
            break
            
        if "RESULTADOS DA BUSCA" in linha_upper:
            capturando = True
            continue
            
        if capturando:
            if linha_upper in ["NENHUM RESULTADO", "NÃO EXISTE AGENDA"]:
                continue
                
            is_date = bool(re.match(r'^\d{2}/\d{2}/\d{4}$', linha_upper)) or \
                      bool(re.match(r'^(SEGUNDA|TERÇA|QUARTA|QUINTA|SEXTA|SÁBADO|DOMINGO).*?\d{4}$', linha_upper))
            is_time = bool(re.match(r'^\d{1,2}h\d{2}$', linha_upper))
            
            if is_date:
                salvar_evento() 
                data_atual = linha
                hora_atual = "HORA NÃO INFORMADA"
            elif is_time:
                salvar_evento() 
                hora_atual = linha
            else:
                buffer_evento.append(linha) 
                
    salvar_evento() 
    return nome_extraido, eventos

# ==========================================
# FUNÇÃO CORE (SCRAPING)
# ==========================================
def processar_agendas(df, data_ini, data_fim, ui_status, ui_progress):
    lista_datas = gerar_lista_datas(data_ini, data_fim)
    
    str_ini_global = data_ini.strftime("%d/%m/%Y")
    str_fim_global = data_fim.strftime("%d/%m/%Y")
    data_ini_enc_global = urllib.parse.quote(str_ini_global, safe='')
    data_fim_enc_global = urllib.parse.quote(str_fim_global, safe='')
    
    textos_whatsapp = []
    total_deputados = len(df)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for idx, row in df.iterrows():
            nome_planilha = str(row["deputado"])
            codigo = str(row["codigo"]).strip()
            
            ui_status.update(label=f"Processando: {nome_planilha} ({idx+1}/{total_deputados})", state="running")
            ui_progress.progress((idx) / total_deputados)
            
            todos_eventos = []
            nome_parlamentar_oficial = None
            
            # --- 1. Varredura Diária ---
            for data_alvo in lista_datas:
                str_data = data_alvo.strftime("%d/%m/%Y")
                data_enc = urllib.parse.quote(str_data, safe='')
                url = f"https://www.camara.leg.br/deputados/{codigo}/agenda?termo=&dataInicial__proxy={data_enc}&dataInicial={data_enc}&dataFinal__proxy={data_enc}&dataFinal={data_enc}"

                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                    texto_bruto = page.evaluate("document.body.innerText")
                    nome_pagina, eventos_dia = extrair_eventos_pagina(texto_bruto)
                    
                    if nome_pagina and not nome_parlamentar_oficial:
                        nome_parlamentar_oficial = nome_pagina.upper()
                        
                    if eventos_dia:
                        todos_eventos.extend(eventos_dia)
                except Exception:
                    pass 

            # --- 2. Período Completo ---
            url_completa = f"https://www.camara.leg.br/deputados/{codigo}/agenda?termo=&dataInicial__proxy={data_ini_enc_global}&dataInicial={data_ini_enc_global}&dataFinal__proxy={data_fim_enc_global}&dataFinal={data_fim_enc_global}"
            try:
                page.goto(url_completa, wait_until="networkidle", timeout=30000)
                texto_bruto = page.evaluate("document.body.innerText")
                nome_pagina, eventos_full = extrair_eventos_pagina(texto_bruto)
                
                if nome_pagina and not nome_parlamentar_oficial:
                    nome_parlamentar_oficial = nome_pagina.upper()
                    
                if eventos_full:
                    todos_eventos.extend(eventos_full)
            except Exception:
                pass

            if not nome_parlamentar_oficial:
                nome_parlamentar_oficial = nome_planilha.upper()

            # =========================================
            # MOTOR ANTI-DUPLICIDADE E ORDENAÇÃO
            # =========================================
            eventos_unicos = []
            hashes_vistos = set()
            for ev in todos_eventos:
                if ev["hash"] not in hashes_vistos:
                    hashes_vistos.add(ev["hash"])
                    eventos_unicos.append(ev)

            # ORDENA CRONOLOGICAMENTE: Primeiro pela Data, depois pela Hora
            eventos_unicos.sort(key=lambda x: (parser_ordenacao_data(x["data"]), parser_ordenacao_hora(x["hora"])))

            # =========================================
            # MONTAGEM FINAL (WHATSAPP) - REGISTRO INDIVIDUAL
            # =========================================
            bloco_msg = f"*{nome_parlamentar_oficial}*\n"
            bloco_msg += f"Período: {str_ini_global} a {str_fim_global}\n\n"
            
            if eventos_unicos:
                agendas_formatadas = []
                for ev in eventos_unicos:
                    # Formata a Data com * para negrito e joga para maiúsculo
                    d_formatado = re.sub(r'\b(\d{2}/\d{2}/\d{4})\b', r'*\1*', ev["data"])
                    if not d_formatado.startswith('*'):
                        d_formatado = f"*{d_formatado.upper()}*"
                    
                    bloco_evento = [d_formatado]
                    
                    # Adiciona a hora com bullet point, se houver
                    if ev["hora"] != "HORA NÃO INFORMADA":
                        bloco_evento.append(f"•\t{ev['hora']}")
                        
                    # Adiciona a descrição do evento
                    bloco_evento.append(ev["descricao"])
                        
                    # Junta as partes do evento atual com quebra de linha
                    agendas_formatadas.append("\n".join(bloco_evento))
                    
                # Junta todos os eventos com DUAS quebras de linha entre eles
                bloco_msg += "\n\n".join(agendas_formatadas) + "\n"
            else:
                bloco_msg += "Nenhuma agenda encontrada para o período.\n"
                
            bloco_msg += "\n" + ("=" * 40) + "\n\n"
            textos_whatsapp.append(bloco_msg)

        browser.close()
        ui_progress.progress(1.0)
        
    return "".join(textos_whatsapp)

# ==========================================
# INTERFACE DO USUÁRIO (FRONTEND)
# ==========================================
st.title("🏛️ Extrator de Agendas Parlamentares")
st.markdown("Busca cruzada ordenada cronologicamente (Data e Hora) com remoção de duplicatas.")
st.divider()

col_lista, col_exec = st.columns([1.2, 2])

with col_lista:
    st.subheader("📋 Sua Lista de Parlamentares")
    
    b1, b2 = st.columns(2)
    b1.button("✅ Marcar Todos", on_click=alternar_todos, args=(True,), use_container_width=True)
    b2.button("❌ Desmarcar Todos", on_click=alternar_todos, args=(False,), use_container_width=True)
    
    with st.container(height=350):
        if not st.session_state.lista_deputados:
            st.warning("A lista está vazia. Adicione parlamentares abaixo.")
            
        for d in st.session_state.lista_deputados:
            key_name = f"chk_{d['codigo']}"
            if key_name not in st.session_state:
                st.session_state[key_name] = True
                
            st.checkbox(f"{d['deputado']} ({d['partido']})", key=key_name)
    
    st.divider()
    
    st.markdown("**Adicionar à Lista**")
    tab1, tab2 = st.tabs(["👤 Buscar Parlamentar", "🏛️ Adicionar Bancada"])
    
    with tab1:
        selecionados_multi = st.multiselect(
            "Pesquise pelo nome",
            options=base_geral,
            format_func=lambda x: f"{x['deputado']} ({x['partido']})",
            placeholder="Digite para buscar..."
        )
        if st.button("➕ Adicionar Específicos", use_container_width=True):
            if selecionados_multi:
                adicionar_a_lista(selecionados_multi)
                st.rerun() 
                
    with tab2:
        nova_sigla = st.text_input("Sigla (ex: PL, PT, MDB)", max_chars=10, label_visibility="collapsed", placeholder="Sigla do Partido")
        if st.button("➕ Adicionar Bancada Inteira", use_container_width=True):
            if nova_sigla:
                adicionar_partido(nova_sigla)
                st.rerun()

with col_exec:
    st.subheader("⚙️ Execução da Extração")
    
    d_col1, d_col2 = st.columns(2)
    with d_col1:
        data_inicio = st.date_input("Data Inicial", format="DD/MM/YYYY")
    with d_col2:
        data_fim = st.date_input("Data Final", format="DD/MM/YYYY")
        
    iniciar_btn = st.button("🚀 Extrair Agendas dos Marcados", use_container_width=True, type="primary")

    if iniciar_btn:
        selecionados = [d for d in st.session_state.lista_deputados if st.session_state.get(f"chk_{d['codigo']}")]
        
        if not selecionados:
            st.error("Por favor, marque pelo menos um parlamentar na lista à esquerda.")
        elif data_inicio > data_fim:
            st.error("A Data Inicial não pode ser maior que a Data Final.")
        else:
            try:
                df_selecionados = pd.DataFrame(selecionados)
                ui_status = st.status(f"Realizando Dupla-Busca para {len(selecionados)} parlamentares...", expanded=True)
                ui_progress = st.progress(0)
                
                resultado_texto = processar_agendas(df_selecionados, data_inicio, data_fim, ui_status, ui_progress)
                
                ui_status.update(label="Extração finalizada com sucesso! (Duplicatas removidas e cronologia ajustada)", state="complete", expanded=False)
                st.success("Tudo pronto! Revise o conteúdo abaixo ou baixe o arquivo.")
                
                st.download_button(
                    label="📥 Baixar Arquivo de WhatsApp (.txt)",
                    data=resultado_texto,
                    file_name=f"WhatsApp_Agendas_{data_inicio.strftime('%d-%m-%Y')}.txt",
                    mime="text/plain",
                    type="primary"
                )
                
                st.text_area("Pré-visualização do Conteúdo:", value=resultado_texto, height=400)
                
            except Exception as e:
                ui_status.update(label="Falha Crítica no Processamento", state="error", expanded=True)
                st.error("Ocorreu um erro interno. Veja os detalhes abaixo:")
                st.code(traceback.format_exc(), language="bash")