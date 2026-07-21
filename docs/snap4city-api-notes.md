# API Smart City Snap4City: osservazioni campo per campo

Riferimento sul backend: appunti campo per campo raccolti interrogando dal vivo gli endpoint
km4city che stanno dietro al server MCP remoto di Snap4City. La §1 tratta la semantica di geocodifica
da cui l'advisor dipende, la §2 le firme degli strumenti remoti che invoca (nomi nudi: una
configurazione a server singolo non aggiunge prefissi), la §3 le prove del perché la geocodifica
diretta è servita localmente.

Sorgente delle specifiche: `ascapi-openapiv3.json` (OAS3, copia su https://www.km4city.org/swagger/external/ascapi-openapiv3.json).
URL di base del backend: `https://www.snap4city.org/superservicemap/api/v1/` (è quello che gli strumenti remoti chiamano internamente; noi non lo tocchiamo direttamente).

---

## §1. Geocodifica: `address_search_location` / km4city `/location/`

Servita dal nostro server MCP **locale** (`mcp_server.py`), che incapsula la ServiceMap pubblica
di km4city — lo strumento remoto con lo stesso nome è difettoso lato server (vedi §3). La forma
della risposta riportata qui sotto è quella restituita da entrambi, quindi il parsing del client
è identico nei due casi.

### Interrogazione
- `search` (stringa): indirizzo o parole chiave del punto di interesse, in testo libero
- `excludePOI` (booleano, default true), `maxresults` (intero, default 100), `logic` ("or"/"and"), `lang`

### Forma della risposta (verificata)

```jsonc
{
  "type": "FeatureCollection",
  "features": [
    {
      "geometry": { "type": "Point", "coordinates": [11.250053, 43.773357] },  // [lng, lat], GeoJSON order
      "properties": { "name": "CHIESA DI SANTA MARIA NOVELLA", "address": "...", "city": "...",
                      "serviceType": "StreetNumber", "civic": "3" }  // civic fields on /location/ house-number hits only
    }
  ]
}
```

`f["geometry"]["coordinates"]` è nell'ordine `[lng, lat]` (convenzione GeoJSON); `properties.name`
è il nome del servizio in maiuscolo così come sta nella knowledge base e può essere `null`.

### Vincoli da conoscere

- **Nessun vincolo territoriale**: l'indice contiene voci di Valencia (ES), del sud della Francia e di Maastricht (NL), quindi una ricerca `"...Firenze"` può restituire 100 risultati fuori regione e nessuno toscano. Non esiste un parametro di vincolo geografico: il client restringe alla città nominata dall'utente ([mcp_tools._narrow_by_city](../src/snap4city_mobility_mcp/mcp_tools.py)) e altrimenti sceglie il candidato più vicino a un punto di ancoraggio ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)) — senza alcun limite di distanza (una soglia di 150 km uccideva richieste legittime ed è stata rimossa: nominare esplicitamente una città lontana è un uso corretto). I dati utilizzabili coprono di fatto la sola Toscana (verificato dal vivo: nessuna via di Brescia o Milano), quindi le interrogazioni fuori regione restituiscono rumore: conviene provare con località toscane.
- **I punti di interesse scavalcano il luogo reale**: con `excludePOI=false`, `"Piazza del Duomo"` restituisce l'azienda `PRIZIO STEFANO` prima della piazza vera. L'advisor geocodifica in due passate (prima `excludePOI=true`, ricadendo sui punti di interesse solo quando la passata sugli indirizzi non trova nulla nella città nominata, [mcp_tools._geocode_address_first](../src/snap4city_mobility_mcp/mcp_tools.py)), e poi preferisce le voci le cui parole dell'etichetta sono un sottoinsieme di quelle cercate ([orchestrator._pick_coord](../src/snap4city_mobility_mcp/orchestrator.py)).
- **Località omonime**: `"Piazza Duomo"` corrisponde anche alle piazze di Castelnuovo e Pietrasanta (90 km di distanza). Una città nominata dall'utente ha la precedenza; altrimenti vince il candidato più vicino all'ancora — la destinazione si ancora all'origine già risolta, l'origine alla posizione GPS dell'utente (verificato dal vivo: senza l'ancoraggio all'origine, "via Pisana 166" partendo da Firenze veniva risolta nella VIA PISANA di Lucca, il primo risultato del server).
- **I numeri civici viaggiano nel testo della ricerca** (non esiste un parametro dedicato): `/location/?search=via Zara 3` mette al primo posto, con il punteggio più alto, proprio la voce `serviceType:"StreetNumber", civic:"3"` (misurato). Ma la scelta per vicinanza all'ancora la seppellirebbe (la destinazione è sempre ancorata): per questo `_normalize_feature` lascia passare `civic` e `serviceType` e [orchestrator._pick_feature](../src/snap4city_mobility_mcp/orchestrator.py) restringe alla corrispondenza esatta sul civico quando il testo dell'utente contiene un numero, ricadendo poi sulle etichette di forma stradale (un punto di interesse con il solo nome, come "LAURA", non deve vincere su "via Laura 11") e solo da ultimo sulla vicinanza all'ancora.
- **Un input di puro rumore produce un HTTP 500**, non una FeatureCollection vuota. Il chiamante deve tollerare i 5xx, e un risultato vuoto o `[]` non è un segnale pulito di "nessuna corrispondenza".
- **Il backend non è deterministico nel tempo**: la stessa stringa può restituire solo risultati stranieri in un momento e quello toscano corretto poco dopo.

---

## §2. Server MCP remoto Snap4City: firme degli strumenti (rilevate il 2026-05-28)

Origine: `GET http://192.168.1.117:8000/apps.json` → `Client(cfg)` → `list_tools()`; [mcp_tools._build_config](../src/snap4city_mobility_mcp/mcp_tools.py) restringe la configurazione al server `native` e sostituisce l'indirizzo interno con `DASHBOARD_URL`. Ambito: il solo `snap4agentic_advisor_native` (25 strumenti esposti).

> **Questa è una fotografia del 2026-05-28.** Conviene rileggere il registro dalla JupyterHub prima di farvi affidamento (il comando è nella §5 del README), nel caso la versione del server sia cambiata.

I nomi compaiono **senza prefisso del server** con una configurazione a server singolo come la nostra: `coordinates_to_address`, non `snap4agentic_advisor_native_coordinates_to_address`. FastMCP antepone il prefisso solo quando fonde più server.

### Strumenti usati dall'advisor (remoti)

| Strumento | Ingresso obbligatorio | Opzioni rilevanti | Scopo |
|---|---|---|---|
| `coordinates_to_address` | `latitude` + `longitude` | — | Geocodifica inversa; dà un nome all'origine ricavata dal GPS |
| `service_search_near_gps_position` | `latitude` + `longitude` | `categories`, `maxdistance` (km), `maxresults` | Punti di interesse più vicini per categoria: parcheggi e destinazioni del tipo "farmacia più vicina" |
| `service_info_dev` | `serviceUri` | `fromTime` | Ultimo dato in tempo reale sui posti liberi di un parcheggio |

La geocodifica diretta (`address_search_location`) e il calcolo dei percorsi (`route`, per tutti i modi) arrivano invece dal server MCP **locale** (§1, §3). Gli strumenti rilevati accettano un parametro opzionale `authentication` (Bearer); la rilevazione non ha mostrato alcun requisito di token, quindi l'advisor lo omette (il backend km4city interrogato è pubblico).

---

## §3. Perché la geocodifica diretta è servita localmente

**`address_search_location` (remoto) è difettoso lato server** — confronto fra interrogazioni diverse, stessa ricerca `via zara 3`: la ServiceMap pubblica restituisce al primo posto `VIA ZARA, FIRENZE` (punteggio 12,64), mentre lo strumento MCP remoto restituisce **zero risultati toscani** (in testa Anversa e Grecia, punteggio 3–7) **e non ordina per punteggio**. Lo schema non espone alcun parametro di ordinamento, di riquadro geografico o di regione, e portare `maxresults` a 5000 non fa comparire il risultato toscano: semplicemente non è nell'insieme restituito. Da qui la scelta dello strumento di geocodifica locale. SuperServiceMap, il backend federato, ha lo stesso difetto di ordinamento ("via zara firenze" mette al primo posto una fermata di Maastricht). Il client può tornare a usarlo cambiando una sola variabile d'ambiente (`S4C_SERVICEMAP_BASE`) se l'ordinamento verrà corretto.
