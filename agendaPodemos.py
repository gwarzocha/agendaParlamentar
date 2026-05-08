import streamlit as st
import pandas as pd
import requests
import traceback
from datetime import datetime

# ==========================================
# CONFIGURAÇÃO DA PÁGINA
# ==========================================
st.set_page_config(
    page_title="Extrator de Agendas Parlamentares (API)",
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
    headers = {"Accept": "application/json"}
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
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
# FUNÇÃO CORE: CONSULTA À API DE EVENTOS
# ==========================================
def processar_agendas_via_api(df, data_ini, data_fim, ui_status, ui_progress):
    str_ini_global = data_ini.strftime("%d/%m/%Y")
    str_fim_global = data_fim.strftime("%d/%m/%Y")
    textos_whatsapp = []
    total_deputados = len(df)
    headers = {"Accept": "application/json"}
    
    for idx, row in df.iterrows():
        nome_planilha = str(row["deputado"]).upper()
        codigo = str(row["codigo"]).strip()
        
        ui_status.update(label=f"Consultando Agendas: {nome_planilha} ({idx+1}/{total_deputados})", state="running")
        ui_progress.progress((idx) / total_deputados)
        
        # Faz a busca direto no "Banco de Dados" da Câmara
        url = f"https://dadosabertos.camara.leg.br/api/v2/deputados/{codigo}/eventos"
        params = {
            "dataInicio": data_ini.strftime("%Y-%m-%d"),
            "dataFim": data_fim.strftime("%Y-%m-%d"),
            "ordem": "ASC",
            "ordenarPor": "dataHoraInicio" # A API já devolve na ordem cronológica correta!
        }
        
        eventos_unicos = []
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 200:
                dados = r.json().get("dados", [])
                
                for ev in dados:
                    # Formatação de Data e Hora ISO
                    dh_inicio = ev.get("dataHoraInicio", "")
                    if "T" in dh_inicio:
                        dt_obj = datetime.fromisoformat(dh_inicio)
                        d_str = dt_obj.strftime("%d/%m/%Y")
                        h_str = dt_obj.strftime("%Hh%M")
                    else:
                        d_str = dh_inicio
                        h_str = "HORA NÃO INFORMADA"
                        
                    # Montagem rica da Descrição
                    tipo = ev.get("descricaoTipo", "")
                    desc = ev.get("descricao", "")
                    sit = ev.get("situacao", "")
                    orgaos_lista = ev.get("orgaos", [])
                    
                    texto_evento = desc
                    if tipo and tipo not in desc:
                        texto_evento = f"{tipo} - {texto_evento}"
                        
                    # Adiciona siglas das comissões se houver
                    siglas_orgaos = [o.get("sigla") for o in orgaos_lista if o.get("sigla")]
                    if siglas_orgaos:
                        texto_evento += f" ({', '.join(siglas_orgaos)})"
                        
                    # Sinaliza se a comissão foi cancelada/convocada
                    if sit and sit.upper() not in ["ENCERRADA", "REALIZADA"]:
                        texto_evento += f" [{sit}]"
                        
                    eventos_unicos.append({
                        "data": d_str,
                        "hora": h_str,
                        "descricao": texto_evento.strip()
                    })
        except Exception:
            pass 
            
        # =========================================
        # MONTAGEM FINAL (WHATSAPP)
        # =========================================
        bloco_msg = f"*{nome_planilha}*\n"
        bloco_msg += f"Período: {str_ini_global} a {str_fim_global}\n\n"
        
        if eventos_unicos:
            agendas_formatadas = []
            for ev in eventos_unicos:
                bloco_evento = [f"*{ev['data']}*"]
                if ev["hora"] != "HORA NÃO INFORMADA" and ev["hora"] != "00h00":
                    bloco_evento.append(f"•\t{ev['hora']}")
                bloco_evento.append(ev["descricao"])
                agendas_formatadas.append("\n".join(bloco_evento))
                
            bloco_msg += "\n\n".join(agendas_formatadas) + "\n"
        else:
            bloco_msg += "Nenhuma agenda encontrada para o período.\n"
            
        bloco_msg += "\n" + ("=" * 40) + "\n\n"
        textos_whatsapp.append(bloco_msg)

    ui_progress.progress(1.0)
    return "".join(textos_whatsapp)

# ==========================================
# INTERFACE DO USUÁRIO (FRONTEND)
# ==========================================
st.title("🏛️ Extrator de Agendas Parlamentares")
st.markdown("Busca instantânea e oficial utilizando a API de Dados Abertos da Câmara (Sem risco de bloqueios).")
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
                ui_status = st.status(f"Conectando à API para {len(selecionados)} parlamentares...", expanded=True)
                ui_progress = st.progress(0)
                
                # Executa o novo motor 100% via API backend
                resultado_texto = processar_agendas_via_api(df_selecionados, data_inicio, data_fim, ui_status, ui_progress)
                
                ui_status.update(label="Extração finalizada instantaneamente!", state="complete", expanded=False)
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
