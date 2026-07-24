# Front-end (dashboard Snap4City)

Una **chat box** in linguaggio naturale, realizzata come `widgetExternalContent`, che
interroga il bridge FastAPI (`api.py`, che incapsula `run_advisor` sulla JupyterHub) e
disegna il percorso restituito su un `widgetMap` adiacente. L'intelligenza sta nel
back-end: è lui che comprende la domanda, geocodifica e calcola il percorso (WKT più
distanza e durata); il front-end si limita a mostrare la risposta e la linea.

## Come la mappa disegna il percorso

Ogni percorso passa dal ramo **manual** di `addCustomTrajectory` del widgetMap
(`mode.routing.manual` su ciascun punto): il widget si limita a congiungere con
segmenti rettilinei i punti che riceve, quindi il front-end gli passa la geometria
calcolata dal back-end e la mappa **non interroga alcun router** — la linea compare
insieme alla risposta, direttamente dal risultato di `/advise`. Un percorso in autobus
trasporta la suddivisione fra tratte a piedi e tratte in autobus come geometrie
separate (`data.routes[].legs`, ritagliate dal back-end a partire dall'unica risposta
del router); a piedi e in auto l'intero WKT viene disegnato come un'unica tratta. Ogni
segmento assume il `color` (una stringa) del punto corrente; un `icon` non vuoto diventa
un segnaposto — le bandierine di partenza e arrivo sui punti geocodificati esatti, il
segnaposto dell'autobus in stile Gea-Night
(`TransferServiceAndRenting_Urban_bus.png`) sui vertici di salita e discesa. Una tratta
in autobus porta di norma la geometria GTFS reale: il back-end sostituisce i segmenti
rettilinei fermata-a-fermata del router con il ritaglio della geometria km4city *tpl*
corrispondente; quando nessuna variante di linea corrisponde entro la tolleranza, quella
tratta ricade sui segmenti rettilinei restituiti dal router.

Un errore che è costato un ciclo di debug (ora codificato nel sorgente): ogni punto deve
portare `mode` e un campo `icon` (stringa vuota = nessun segnaposto) — se manca, la mappa
va in errore dentro `addCustomTrajectoryToMap` (`Cannot read properties of undefined
(reading 'length')`).

## File

- `mobility_advisor_dashboard.html` — da incollare in un `widgetExternalContent`.

## Inserimento nella dashboard

1. Aggiungere un **widgetMap** e annotarne l'identificativo (per esempio
   `w_Map_xxxx_widgetMapyyyyy`).
2. Aggiungere un **widgetExternalContent**. In "More options" attivare **Enable
   CKEditor** e incollare l'intero contenuto di `mobility_advisor_dashboard.html` nella
   casella di CKEditor.
3. Nello script incollato, impostare `MAP_WIDGET_ID` con l'identificativo del widgetMap e
   `BRIDGE_BASE` con l'indirizzo a cui il bridge FastAPI (`api.py`) è raggiungibile **dal
   browser** (dipende dal tipo di deploy — vedi sotto).

## Avvio del bridge (JupyterHub)

Il bridge ha bisogno di Llama4 (raggiungibile solo dalla JupyterHub) e dei due server MCP di
Snap4City (advisor nativo + mobility), ora ospitati sul server Snap4City. Serve quindi **un
solo processo** sulla JupyterHub:

```
uvicorn api:app --host 0.0.0.0 --port 8010
```

Per collaudare in locale una copia del server MCP mobility: avviarlo con
`python -m snap4city_mobility_mcp.mcp_server` (serve `:8020`) e puntare il client con
`export S4C_LOCAL_MCP_URL=http://127.0.0.1:8020/mcp`.

**Job + poll**: la POST si limita ad *avviare* il turno (risponde subito con
`{job_id}`) e il widget interroga `GET /advise/{job_id}` finché non ottiene 200. Un
turno in autobus richiede circa 50–70 s e la catena di proxy davanti al bridge
interrompe qualunque singola richiesta oltre i ~60 s — e i byte di heartbeat non
aiutano, perché `jupyter-server-proxy` bufferizza per intero il corpo di una risposta
non SSE. Da non riportare mai a un'unica richiesta lunga.

Ogni 202 trasporta `{"status":"pending","stage":...,"elapsed_s":...}`; il widget
riscrive da lì la propria bolla di attesa (`STAGE_TEXT`), così un turno in autobus mostra
*"Calcolo il percorso in bus… 34s"* invece di una bolla muta per quasi un minuto. Una
fase non riconosciuta ricade sul testo generico, quindi una nuova fase lato back-end non
può mai svuotare la riga.

Il 200 finale è il JSON per il widget `{status, request_type, data, messages}`: vanno
verificati la presenza di `data.wkt` (la LINESTRING), `data.distance_km`,
`data.duration` e `data.mode`, e che `messages[-1].content` sia la risposta in italiano.

### Esporre il bridge al browser

Il browser raggiunge il bridge **same-origin** attraverso `jupyter-server-proxy`:
`BRIDGE_BASE = https://www.snap4city.org/jupyterhub/user/<account>/proxy/8010`. L'origine
deve coincidere esattamente con quella della dashboard (`www.` compreso): un'origine
diversa fa scattare una richiesta di preflight CORS che il proxy reindirizza alla pagina
di login, e il browser la rifiuta.

Installare quell'estensione sul `jupyter_server` datato della JupyterHub richiede
attenzione, perché un'installazione disattenta aggiorna lo stack Jupyter di base e rende
il singleuser server non avviabile:

1. Fissare la versione maggiore: `jupyter-server-proxy>=3.2,<4` (la 4.x richiede
   `jupyter_server>=2`).
2. Installare con `pip install --no-deps`, così nulla aggiorna
   `jupyter_server`/`jupyterlab`/`notebook`.
3. Aggiungere l'unica dipendenza reale che `--no-deps` salta: `pip install aiohttp`.
4. **Verificare senza toccare il server in esecuzione**: `jupyter server extension list`
   deve mostrare l'estensione come `OK`, poi si avvia un server usa e getta
   (`jupyter server --port=9999 --no-browser --ServerApp.token=''`) e si controlla che
   carichi senza traceback. Solo a quel punto si riavvia il server principale dal Hub
   Control Panel.

La configurazione CORS in `api.py` è permissiva (`*`) perché pensata per lo sviluppo e
andrebbe ristretta in un deploy di produzione.

## GPS (posizione dell'utente)

A ogni invio viene chiamata `navigator.geolocation.getCurrentPosition` (timeout 5 s,
cache 60 s, bassa precisione) e la posizione viene inviata come `gps: {lat, lng}` —
`null` in caso di rifiuto, API non disponibile o timeout; in quel caso il back-end si
comporta esattamente come prima (chiede l'origine quando manca). Un `PERMISSION_DENIED`
viene ricordato per la sessione, così l'utente non se lo ritrova richiesto a ogni turno.

Due requisiti dell'ambiente, entrambi **fuori dal controllo di questo file**:

- **Contesto sicuro**: la geolocalizzazione funziona solo su pagine HTTPS (la dashboard
  è in HTTPS, quindi va bene).
- **Permesso dell'iframe**: il widget vive nell'iframe della dashboard; se l'iframe
  genitore non ha `allow="geolocation"` la richiesta di permesso non compare mai e
  `getCurrentPosition` fallisce con codice 1 — il widget ricade silenziosamente sul
  flusso senza GPS. Se gli iframe dei widgetExternalContent di Snap4City abbiano quel
  permesso è un'impostazione della piattaforma: si verifica con i DevTools (eseguendo
  `navigator.permissions.query({name:'geolocation'})` nel contesto dell'iframe) e, se
  risulta bloccato, va chiesto agli amministratori della piattaforma Snap4City di
  abilitarlo.

## Note

- La bolla di risposta è `messages[-1].content` (standard OpenAI, nessun campo `answer`
  aggiuntivo).
- Conversazione a più turni: il front-end conserva `response.messages` e li rispedisce
  come `history`.
- Tutti e tre i modi sono calcolati dallo strumento locale `route` del back-end (router
  What-If GraphHopper) e disegnati dalla sua geometria — verde a piedi, blu in auto,
  arancione per la tratta in autobus con i segnaposto alle fermate di salita e discesa.
  A piedi e in auto sono itinerari **monomodali** (un'unica geometria); quello con il
  trasporto pubblico è **multimodale**, quindi arriva suddiviso in `legs` ed è l'unico
  disegnato a due colori. Un itinerario di trasporto pubblico interamente pedonale
  (tragitto breve: camminare batte qualunque autobus) torna ri-etichettato come percorso
  a piedi, quindi la mappa disegna una normale linea verde.
- **Selettore dei percorsi**: un turno a più modi (nessun modo indicato → 2-3 percorsi
  restituiti) riempie il dock `#advChips` (una barra fissa fra la chat e la riga di
  input) con una chip per percorso, etichettata con il solo nome del modo ("A piedi" /
  "In auto" / "In autobus" — distanza e durata sono già nel testo della risposta), più
  "Mostra tutte". Toccando una chip la mappa viene ridisegnata con **solo** quel
  percorso, viene mostrato come bolla di chat il suo blocco passo-passo
  (`data.routes[].detail`, già formattato dal back-end: fermate e orari per l'autobus,
  indicazioni stradali per a piedi e auto), vengono commutati i segnaposto dei parcheggi
  (visibili in auto, nascosti a piedi e in autobus) e vengono sostituiti quelli dei
  servizi lungo il percorso con l'insieme relativo a quel percorso.
  La selezione è **puramente locale** — nessun nuovo turno verso il back-end (ricalcolare
  l'autobus costerebbe altri 30-45 s) — e nella `history` conservata entra solo la riga
  di eco dell'utente ("Scelgo l'opzione: …"): basta a mantenere in contesto il modo
  scelto per le domande successive, mentre il blocco di dettaglio resta una bolla e non
  finisce in tutti i prompt seguenti.
  Ogni turno che porta percorsi **sostituisce** il contenuto del dock (un turno a
  percorso singolo lo svuota); il dock si nasconde quando è vuoto.
- **Segnaposto dei servizi lungo il percorso**: quando l'utente ha chiesto di vedere una
  categoria lungo il tragitto, ogni percorso porta `data.routes[].services` e la mappa li
  disegna con la stessa pipeline `addSelectorPin` dei parcheggi, in viola — è il widget
  a risolvere ogni `serviceUri` e a disegnare l'icona della categoria del servizio (il
  segnaposto di una farmacia ha l'aspetto di una farmacia senza una riga di codice qui).
  Finché sono mostrati tutti i percorsi l'insieme dei segnaposto è l'unione deduplicata;
  una chip lo restringe alla lista di quel percorso (per l'autobus il back-end elenca
  solo i servizi vicini alle fermate e lungo le tratte a piedi).
- **I segnaposto dei parcheggi** si rimuovono con l'evento `removeSelectorPin`
  **indicizzato per `desc`** (verificato in `widgetMap.php`, disit/dashboard-builder: la
  mappa indicizza per `passedData.desc` il livello di ciascun segnaposto e il gestore
  della rimozione dereferenzia anche `passedData.query` senza protezioni) — la
  cancellazione deve rispedire gli stessi `desc`, `query` e `display` usati in
  inserimento. Inviare solo `{query, queryType}` rende la rimozione una no-op
  silenziosa e i segnaposto si accumulano. I segnaposto dei servizi seguono la stessa
  regola, con una propria contabilità.
