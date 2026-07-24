# snap4city-mobility-mcp

**Client MCP con orchestrazione LangGraph** per il server *mobility advisor* remoto della piattaforma Snap4City. UNIFI — *Sistemi Distribuiti, elaborato di Tipo A*.

L'utente pone una domanda di viaggio → un grafo LangGraph **deterministico** (`understand → execute → respond`) la risolve: il modello **Llama4** di Snap4City si limita a estrarre i parametri della richiesta (origine/destinazione/modo), mentre il codice Python guida in modo deterministico gli strumenti MCP — geocodifica e calcolo percorsi (tutti i modi) sul **server MCP mobility** (`mcp_server.py`, che incapsula la ServiceMap pubblica km4city e il router What-If GraphHopper di Snap4City, ora ospitato sul server Snap4City), geocodifica inversa / ricerca di servizi per prossimità / parcheggi in tempo reale sul server advisor nativo — e compone la risposta. Il modello non invoca mai strumenti di propria iniziativa. Il risultato è il JSON che il widget della dashboard Snap4City disegna. Entrambi i server MCP (advisor nativo e mobility) fanno parte della piattaforma Snap4City e sono ospitati sul server Snap4City (rete interna, raggiungibile direttamente dalla JupyterHub); questo progetto fornisce **client + orchestratore LangGraph + bridge FastAPI (`api.py`) + front-end della dashboard (`frontend/`) + il server MCP mobility (`mcp_server.py`)**.

La relazione scritta dell'elaborato è in [relazione/](relazione/); i diagrammi dell'architettura in [docs/diagrams/](docs/diagrams/), le schermate della dashboard in [screenshots/](screenshots/) e gli output reali end-to-end in [examples/](examples/).

---

## 1. Stato

Funzionante end-to-end sulla **JupyterHub di Snap4City** (accesso da browser, MCP raggiunto direttamente sulla rete interna). Il server MCP remoto di Snap4City è collegato via HTTP Streamable e il **client per l'LLM Llama4** (`src/snap4city_mobility_mcp/llm.py`, endpoint `llama4-agentic-inference`) è attivo in quell'ambiente.

L'advisor deterministico risponde a **domande di percorso punto-punto** — due itinerari **monomodali** (a piedi, in auto) e uno **multimodale** con il trasporto pubblico (tratto pedonale → tratte in autobus → tratto pedonale) — risolvendo gli estremi anche in base alla posizione GPS:

- I **luoghi nominati** vengono geocodificati senza vincolo di regione; una città indicata dall'utente ha sempre la precedenza e, quando il GPS del browser è disponibile, fra candidati equivalenti vince quello **più vicino all'utente**. I dati km4city utilizzabili coprono di fatto la sola Toscana: conviene provare con località toscane.
- Se **manca l'origine** ("portami al Duomo") si usa la **posizione GPS** dell'utente (geocodificata a ritroso una sola volta, così la risposta può dire *"dalla tua posizione"*); senza GPS l'advisor chiede il punto di partenza.
- Le **destinazioni per categoria generica** ("la farmacia più vicina") si risolvono con lo strumento remoto `service_search_near_gps_position`: il servizio più vicino di quella categoria km4city attorno all'utente (o attorno alla città nominata, se il GPS manca).
- I **servizi lungo il percorso** ("con le farmacie lungo il percorso") vengono cercati campionando punti di ancoraggio sulla geometria calcolata ed eseguendo una ricerca per prossimità attorno a ciascuno; i risultati sono poi associati al singolo modo di trasporto.
- La **ricerca di soli servizi vicini** ("mostrami le farmacie qui intorno") non calcola alcun percorso: attorno al centro — la posizione GPS oppure un indirizzo o una città indicati — cerca fino a 50 servizi della categoria richiesta e li mostra come **pin sulla mappa** (nessuna linea; se manca la categoria o il luogo, l'advisor lo chiede).

Lo strumento remoto `routing` è stato dismesso lato client: **tutto il calcolo dei percorsi** (a piedi / auto / trasporto pubblico) passa dallo strumento locale `route`, che incapsula il router What-If GraphHopper di Snap4City. Lo strumento remoto non ha mai restituito trasporto pubblico reale per `public_transport` e il suo backend km4city richiedeva un'intera catena di tentativi per gestire le risposte vuote transitorie; il router What-If è insieme corretto e — per i profili a piedi e auto — più rapido. Il server remoto continua a fornire geocodifica inversa, ricerca di servizi per prossimità e dati sui parcheggi in tempo reale.

---

## 2. Installazione

Servono **Python ≥ 3.10** (`.python-version` fissa la 3.10) e [`uv`](https://github.com/astral-sh/uv) (`pip install uv`):

```powershell
git clone https://github.com/Zhengjunliang/snap4city-mobility-mcp.git
cd snap4city-mobility-mcp
uv sync          # creates .venv/ and installs the lockfile's exact versions
uv run pytest -q # local mock tests: no LLM / MCP needed, runs anywhere
```

Sulla **JupyterHub** (l'unico ambiente in cui l'advisor gira davvero, vedi §3) `uv` di solito non c'è: conviene creare un ambiente conda con **Python 3.11** (`s4c`) — il kernel predefinito 3.9 è troppo vecchio per fastmcp — e installare con `pip install -e .`.

L'**LLM Llama4** risponde **solo dalla JupyterHub**: le credenziali dell'account funzionale vanno messe lì in un file `user_credentials.json` (`{"username": "...", "password": "..."}`) — è escluso da git, quindi va caricato a mano nella radice del progetto. Il client lo cerca in `S4C_CREDENTIALS_FILE` → directory di lavoro → radice del progetto.

---

## 3. Esecuzione (JupyterHub)

Il server MCP remoto è sulla rete interna e viene raggiunto direttamente dalla JupyterHub; l'orchestratore punta di default a `http://192.168.1.117:8000` (modificabile con `S4C_DASHBOARD_URL`). Accesso: snap4city.org → *Strumenti di sviluppo* → *Jupyter Hub - Python*

Serve **un solo processo** sulla JupyterHub, nell'ambiente `s4c`: il bridge dell'advisor. Il server MCP mobility (geocodifica + `route` per tutti i modi) è ora ospitato sul server Snap4City, quindi non va più avviato a mano.

```bash
uvicorn api:app --host 0.0.0.0 --port 8010    # bridge — :8010
```

- **Bridge dell'advisor** (`:8010`): guida l'LLM ed entrambi i server MCP di Snap4City — l'advisor nativo (scoperto via `apps.json`) e il server mobility (all'URL fisso `S4C_LOCAL_MCP_URL`, default `http://192.168.1.117:8000/snap4agentic-mobility-advisor/mcp`). Il browser lo raggiunge *same-origin* attraverso `jupyter-server-proxy` (procedura di installazione e configurazione in [frontend/README.md](frontend/README.md)). Per collaudare in locale una copia del server mobility: `python -m snap4city_mobility_mcp.mcp_server` (serve `:8020`) ed `export S4C_LOCAL_MCP_URL=http://127.0.0.1:8020/mcp`.

Il front-end è una **chat box** in linguaggio naturale sulla dashboard Snap4City (`frontend/mobility_advisor_dashboard.html`, un `widgetExternalContent`) che dialoga con il bridge, con il percorso disegnato su un `widgetMap` adiacente.

### Il protocollo del bridge: job + poll

`POST /advise` **avvia** il turno e risponde subito `{"job_id": ...}`; `GET /advise/{job_id}` restituisce `202` mentre il calcolo è in corso e `200` con il JSON per il widget quando è finito.

Ogni `202` trasporta anche la **fase** (`understand` → `geocode` → `routing` / `routing_bus` → `respond`) e il tempo trascorso, così la chat dichiara che cosa sta facendo invece di mostrare una bolla di attesa muta. Fase e identificativo del job vivono solo nel livello di trasporto: non entrano mai nel JSON destinato al widget.

Ogni turno sovrascrive `outputs.txt` con il JSON completo dell'output e `debug.log` con la diagnostica a livello di strumenti (entrambi esclusi da git, entrambi nella directory di lavoro): sono il primo posto da guardare quando un turno non disegna alcun percorso. Alcuni esempi di quell'output sono allegati in [examples/](examples/).

### Il payload per il widget

```json
{
  "status": "success",
  "request_type": "route",
  "data": {
    "wkt": "LINESTRING(11.255 43.773, ...)",   // FULL geometry — map widget draws this
    "distance_km": 0.679, "duration": "0:10:00", "mode": "foot"
  },
  "messages": [ ... updated conversation; LAST assistant turn = the reply text ... ]
}
```

Il testo della risposta è **l'ultimo turno `assistant` in `messages`** (standard OpenAI): `data` contiene il `wkt` completo, `distance_km`, `duration` e `mode`, più una lista `routes` (una voce per modo di trasporto; un percorso in autobus trasporta anche la geometria delle tratte in `legs` per la suddivisione sulla mappa, e ogni percorso porta con sé una stringa `detail` già formattata e i propri `services`). Il front-end conserva `messages` e li rispedisce come `history` al turno successivo, e a ogni turno invia la geolocalizzazione del browser come `gps: {lat, lng}` (oppure `null`). Le domande fuori ambito (comprese quelle di consultazione della rete, come elenchi di linee od orari) ricevono una risposta esplicita di non supporto.

**Modi di trasporto.** Quando la domanda non ne indica uno (*"da Piazza del Duomo a Santa Croce"*), tutti e tre vengono calcolati **in parallelo** — a piedi, in auto e con il trasporto pubblico — così la risposta li confronta e la mappa disegna una linea per ciascuno. I primi due sono itinerari **monomodali** (un unico mezzo dall'origine alla destinazione, quindi un'unica geometria); quello con il trasporto pubblico è **multimodale** — tratto pedonale fino alla fermata di salita, una o più tratte in autobus, tratto pedonale finale — e per questo viaggia suddiviso in `legs`, ciascuna con la propria geometria e il proprio mezzo. Il turno risponde una sola volta, quando tutti e tre sono pronti: il tempo totale è quello del più lento, oggi quello in autobus, perché il router What-If online ricostruisce il grafo del trasporto pubblico a ogni richiesta `vehicle=bus` (~30–45 s, una latenza accettata — l'indicatore di fase mantiene visibile l'attesa); i profili a piedi e auto rispondono in meno di un secondo. Indicando un modo (*"a piedi"*) si calcola solo quello, che quindi non paga mai la latenza dell'autobus. Un **orario di partenza** fornito dall'utente (*"alle 18"*, *"domani alle 9"*) diventa la finestra sugli orari del trasporto pubblico; un orario di *arrivo* non è supportato (il servlet What-If non espone `arrive_by`).

---

## 4. Struttura del progetto

```
snap4city-mobility-mcp/
├── LICENSE                     # MIT
├── pyproject.toml              # uv-managed project file
├── uv.lock                     # exact-version lockfile (committed)
├── api.py                      # FastAPI bridge for the dashboard chat box (job/poll: POST /advise + GET /advise/{job_id})
├── frontend/                   # Snap4City dashboard front-end (widgetExternalContent chat box + widgetMap)
├── relazione/                  # elaborato report — LaTeX source + PDF
├── docs/
│   ├── diagrams/               # UML: PlantUML sources (.puml) + rendered .png
│   └── snap4city-api-notes.md  # field-by-field observations of the real API
├── screenshots/                # dashboard screenshots of the working advisor
├── examples/                   # real widget-JSON outputs captured from live turns
├── scripts/                    # delivery packaging
├── tests/                      # local mock unit tests (no LLM / MCP needed)
└── src/
    └── snap4city_mobility_mcp/    # client package + mobility MCP server source — both MCP servers are hosted on the Snap4City server
        ├── mcp_tools.py           # client MCP layer: Client config, exec_tool, two-pass geocode, result parsers
        ├── mcp_server.py          # our mobility MCP server (hosted on the Snap4City server): forward geocode (public km4city ServiceMap) + `route` for all modes (What-If GraphHopper)
        ├── orchestrator.py        # deterministic Langgraph graph: understand → execute → respond; run_advisor
        ├── gtfs_shapes.py         # swaps bus ride-leg chords for real km4city GTFS shapes
        ├── geo.py                 # haversine + WKT helpers, shared by the graph and the local server
        ├── llm.py                 # Llama4Client — Snap4City agentic LLM (llama4-agentic-inference, OpenAI-compatible)
        └── token_manager.py       # vendored auth util (OAuth2 token cache/refresh) from the Snap4City reference example
```

---

## 5. Strumenti utilizzati (tutti sul server Snap4City)

Il server remoto `snap4agentic_advisor_native` (della piattaforma Snap4City) fornisce la **geocodifica inversa** (`coordinates_to_address`, che dà un nome all'origine ricavata dal GPS), la **ricerca di servizi per prossimità** (`service_search_near_gps_position`, usata per i parcheggi vicino alla destinazione, per le destinazioni del tipo "farmacia più vicina", per i servizi lungo il percorso e per la ricerca di soli servizi vicini senza percorso) e i **parcheggi in tempo reale** (`service_info_dev`, posti liberi per singolo parcheggio). Lo si raggiunge tramite la scoperta automatica della dashboard (`http://192.168.1.117:8000/apps.json` → `Client(config)`), restringendo la configurazione a quel solo server: FastMCP antepone il prefisso ai nomi degli strumenti solo quando fonde **più** server in un'unica configurazione, quindi con un solo server gli strumenti si invocano con il nome nudo.

**La geocodifica diretta e il calcolo dei percorsi sono serviti dal server MCP mobility** (`mcp_server.py`, ospitato sul server Snap4City): `address_search_location` (che incapsula la ServiceMap **pubblica** di km4city, perché quella remota è difettosa lato server) e `route` (`vehicle="foot"|"car"|"bus"` più coordinate di partenza e arrivo e un `startdatetime` opzionale, che incapsula il router What-If GraphHopper). Il client vi si collega come **client separato** a server singolo (`S4C_LOCAL_MCP_URL`); è proprio tenerlo separato che preserva i nomi nudi degli strumenti remoti.

Il nodo deterministico `execute` li concatena in Python — risoluzione dell'origine (geocodifica o punto GPS) → risoluzione della destinazione (geocodifica o servizio più vicino per categoria) → calcolo di ciascun modo → eventuale campionamento dei servizi lungo la geometria — e ogni chiamata passa da `mcp_tools.exec_tool`.

---

## 6. Licenza

**MIT** — vedi [LICENSE](LICENSE).

`src/snap4city_mobility_mcp/token_manager.py` (gestione della cache e del rinnovo del token OAuth2) è adattato dal notebook di riferimento Snap4City e qui ridistribuito nell'ambito di questo elaborato accademico; tutto il resto del codice è originale.
