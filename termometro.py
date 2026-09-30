"""
Termometro de regime - Exhaustion EA
Calcula, uma vez por dia, se cada par esta num regime de tendencia forte.
Saida: termometro.csv  (par;tendencia;nota;er_pct;juros_pct;cot_pct;vix_pct;data)

Componentes (cada um vira um percentil 0-1 contra a propria historia):
  er_pct    persistencia de tendencia do par (efficiency ratio 60 dias)
  juros_pct velocidade da divergencia de juros entre as duas economias (6 meses)
  cot_pct   velocidade da mudanca de posicionamento especulativo (CFTC, 13 semanas)
  vix_pct   estresse global (VIX, media de 20 dias)
nota = quantos componentes estao no extremo (>= 0.90). Fonte que falhar fica vazia e nao conta.
tendencia = +1 se o fechamento esta acima da media de 100 dias, -1 se abaixo.
"""
import csv, io, sys, zipfile, datetime as dt, statistics as st, urllib.request, time

PARES = ["AUDUSD","EURCHF","GBPNZD","CADJPY","EURAUD","AUDCAD","GBPCAD","EURNZD"]
EXTREMO = 0.90
HOJE = dt.date.today()

def baixar(url, timeout=60, tentativas=3):
    # User-Agent modificado simulando navegador real para evitar rate limit e bloqueios de bot
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    req = urllib.request.Request(url, headers=headers)
    
    for tentativa in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if tentativa < tentativas - 1:
                print(f"    [tentativa {tentativa+1}/{tentativas} falhou, aguardando 5s...] {url.split('?')[0]}")
                time.sleep(5)
            else:
                raise e # Repassa o erro se falhar em todas as tentativas

def fred(serie):
    """Serie do FRED sem chave de API. Retorna lista [(date, valor)] ordenada."""
    txt = baixar(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie}").decode()
    out = []
    for i, linha in enumerate(csv.reader(io.StringIO(txt))):
        if i == 0 or len(linha) < 2: continue
        try: out.append((dt.date.fromisoformat(linha[0]), float(linha[1])))
        except ValueError: pass          # FRED usa "." para dia sem dado
    return out

def percentil(atual, historico):
    h = [x for x in historico if x is not None]
    if atual is None or len(h) < 30: return None
    return sum(1 for x in h if x <= atual) / len(h)

# ---------------------------------------------------------------- CAMBIO (FRED)
# unidades por dolar -> convertemos tudo para "dolares por 1 unidade da moeda"
FX = {"JPY": ("DEXJPUS", True), "CAD": ("DEXCAUS", True), "CHF": ("DEXSZUS", True),
      "AUD": ("DEXUSAL", False), "EUR": ("DEXUSEU", False), "NZD": ("DEXUSNZ", False),
      "GBP": ("DEXUSUK", False)}

def carregar_fx():
    usd = {}
    for moeda, (serie, inverte) in FX.items():
        try:
            usd[moeda] = {d: (1/v if inverte else v) for d, v in fred(serie) if v > 0}
        except Exception as e:
            print(f"[aviso] cambio {moeda} indisponivel: {e}")
    return usd

def serie_par(usd, par):
    b, q = par[:3], par[3:]
    get = lambda m, d: 1.0 if m == "USD" else usd.get(m, {}).get(d)
    datas = sorted(set(usd.get(b, {}) if b != "USD" else usd.get(q, {})) &
                   set(usd.get(q, {}) if q != "USD" else usd.get(b, {})))
    return [(d, get(b, d) / get(q, d)) for d in datas if get(b, d) and get(q, d)]

def efficiency_ratio(px, n=60):
    if len(px) <= n: return None
    liquido = abs(px[-1] - px[-1-n])
    caminho = sum(abs(px[i] - px[i-1]) for i in range(len(px)-n, len(px)))
    return liquido / caminho if caminho > 0 else None

# ---------------------------------------------------------------- JUROS (FRED/OECD)
# taxa de curtissimo prazo (overnight/politica). Mensal, com 1-2 meses de atraso.
JUROS = {"USD": ["DFF", "IRSTCI01USM156N"], "EUR": ["ECBDFR", "IRSTCI01EZM156N"],
         "JPY": ["IRSTCI01JPM156N"], "GBP": ["IRSTCI01GBM156N"], "CHF": ["IRSTCI01CHM156N"],
         "AUD": ["IRSTCI01AUM156N"], "CAD": ["IRSTCI01CAM156N"], "NZD": ["IRSTCI01NZM156N"]}

def carregar_juros():
    out = {}
    for moeda, series in JUROS.items():
        for s in series:
            try:
                dados = fred(s)
                if dados and (HOJE - dados[-1][0]).days < 200:
                    mensal = {}
                    for d, v in dados: mensal[(d.year, d.month)] = v   # ultimo valor de cada mes
                    out[moeda] = mensal; break
            except Exception as e:
                print(f"[aviso] juros {moeda} via {s}: {e}")
        if moeda not in out: print(f"[aviso] juros {moeda} indisponivel")
    return out

def juros_componente(juros, par, ate):
    b, q = par[:3], par[3:]
    if b not in juros or q not in juros: return None
    meses = sorted(k for k in set(juros[b]) & set(juros[q]) if k <= (ate.year, ate.month))
    if len(meses) < 60: return None
    dif = [juros[b][m] - juros[q][m] for m in meses]
    vel = [abs(dif[i] - dif[i-6]) for i in range(6, len(dif))]
    return percentil(vel[-1], vel[-240:])

# ---------------------------------------------------------------- COT (CFTC)
COT = {"JPY": "097741", "CAD": "090741", "AUD": "232741", "NZD": "112741",
       "GBP": "096742", "CHF": "092741", "EUR": "099741"}

def carregar_cot(anos=10):
    pos = {m: {} for m in COT}
    for ano in range(HOJE.year - anos, HOJE.year + 1):
        try:
            z = zipfile.ZipFile(io.BytesIO(baixar(f"https://www.cftc.gov/files/dea/history/deacot{ano}.zip", 120)))
            txt = z.read(z.namelist()[0]).decode("latin-1")
        except Exception as e:
            print(f"[aviso] COT {ano}: {e}"); continue
        leitor = csv.DictReader(io.StringIO(txt))
        col = lambda frag: next((c for c in leitor.fieldnames if frag in c), None)
        c_cod, c_data = col("Contract Market Code"), col("YYYY-MM-DD")
        c_oi, c_l, c_s = col("Open Interest (All)"), col("Noncommercial Positions-Long (All)"), col("Noncommercial Positions-Short (All)")
        if not all([c_cod, c_data, c_oi, c_l, c_s]): print(f"[aviso] COT {ano}: colunas nao reconhecidas"); continue
        for r in leitor:
            for moeda, cod in COT.items():
                if r[c_cod].strip() == cod:
                    try:
                        oi = float(r[c_oi]); d = dt.date.fromisoformat(r[c_data].strip())
                        pos[moeda][d] = (float(r[c_l]) - float(r[c_s])) / oi if oi > 0 else 0.0
                    except ValueError: pass
    return pos

def cot_componente(cot, par, ate):
    b, q = par[:3], par[3:]
    pega = lambda m: {} if m == "USD" else cot.get(m, {})
    pb, pq = pega(b), pega(q)
    if (b != "USD" and not pb) or (q != "USD" and not pq): return None
    datas = sorted(d for d in (set(pb) | set(pq)) if d <= ate and (b == "USD" or d in pb) and (q == "USD" or d in pq))
    if len(datas) < 120: return None
    liq = [(pb.get(d, 0.0) if b != "USD" else 0.0) - (pq.get(d, 0.0) if q != "USD" else 0.0) for d in datas]
    vel = [abs(liq[i] - liq[i-13]) for i in range(13, len(liq))]
    return percentil(vel[-1], vel[-520:])

# ---------------------------------------------------------------- VIX (FRED)
def vix_componente(vix, ate):
    v = [x for d, x in vix if d <= ate]
    if len(v) < 300: return None
    med20 = [st.mean(v[i-20:i]) for i in range(20, len(v)+1)]
    return percentil(med20[-1], med20[-2520:])

# ---------------------------------------------------------------- TABELA
def tabela(usd, juros, cot, vix, ate):
    linhas = []
    pv = vix_componente(vix, ate) if vix else None
    for par in PARES:
        s = [(d, p) for d, p in serie_par(usd, par) if d <= ate]
        if len(s) < 300:
            linhas.append([par, 0, 0, None, None, None, pv]); continue
        px = [p for _, p in s]
        tend = 1 if px[-1] > st.mean(px[-100:]) else -1
        ers = [efficiency_ratio(px[:i]) for i in range(max(61, len(px)-2520), len(px)+1, 5)]
        per = percentil(ers[-1], ers)
        pj = juros_componente(juros, par, ate)
        pc = cot_componente(cot, par, ate)
        comps = [per, pj, pc, pv]
        nota = sum(1 for c in comps if c is not None and c >= EXTREMO)
        linhas.append([par, tend, nota, per, pj, pc, pv])
    return linhas

def fmt(x): return "" if x is None else f"{x:.2f}"

def main():
    usd = carregar_fx()
    juros = carregar_juros()
    cot = carregar_cot()
    
    try: 
        vix = fred("VIXCLS")
    except Exception as e: 
        print(f"[aviso] VIX: {e}"); vix = []
    
    # ---------------------------------------------------------------- TRAVA DE SEGURANÇA
    valores_validos = [s for s in usd.values() if s]
    if not valores_validos:
        print("Erro critico: Nenhum dado de cambio foi baixado (falha de rede/rate limit). Abortando.")
        sys.exit(1)
        
    ultimo = max(max(s) for s in valores_validos)
    # -----------------------------------------------------------------------------------
    
    with open("termometro.csv", "w") as f:
        f.write("par;tendencia;nota;er_pct;juros_pct;cot_pct;vix_pct;data\n")
        for l in tabela(usd, juros, cot, vix, ultimo):
            # data = dia do CALCULO (o EA usa para saber se a tabela esta atualizada).
            # O cambio do Fed sai semanalmente, entao a ultima cotacao pode ter ate ~10 dias.
            f.write(f"{l[0]};{l[1]};{l[2]};{fmt(l[3])};{fmt(l[4])};{fmt(l[5])};{fmt(l[6])};{HOJE.isoformat()}\n")
    print(f"ultima cotacao usada: {ultimo}")
    print(open("termometro.csv").read())
    
    if "--historico" in sys.argv:           # tabela semanal desde 2018, para a validacao
        with open("termometro_historico.csv", "w") as f:
            f.write("data;par;tendencia;nota;er_pct;juros_pct;cot_pct;vix_pct\n")
            d = dt.date(2018, 1, 5)
            while d <= ultimo:
                for l in tabela(usd, juros, cot, vix, d):
                    f.write(f"{d.isoformat()};{l[0]};{l[1]};{l[2]};{fmt(l[3])};{fmt(l[4])};{fmt(l[5])};{fmt(l[6])}\n")
                d += dt.timedelta(days=7)
        print("historico gravado")

if __name__ == "__main__":
    main()
