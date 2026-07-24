# Examples — output reali dell'advisor

File JSON con l'output completo ("widget JSON") dei turni reali dell'advisor, catturati
durante le prove sulla **Snap4City JupyterHub** (bridge attivo). Ogni turno scrive
`outputs.txt` nella cwd del bridge (`.gitignore`d): questi file sono copie di quei
risultati, uno per scenario, numerati come le schermate in `screenshots/`.

I sotto-stati interattivi delle chip (a piedi / in auto / in autobus / mostra tutte) **non**
hanno un file proprio: sono viste lato client dello stesso payload di `S01-tre-modi.json`,
non un nuovo turno verso il bridge.

## File presenti

| File | Scenario | Contenuto principale |
|---|---|---|
| `S01-tre-modi.json` | Tre modi (nessun modo indicato) | tre rotte sullo stesso tragitto (a piedi, auto, trasporto pubblico), ciascuna con la propria geometria e il proprio `detail`; da qui derivano le viste delle chip |
| `S06-modo-singolo-piedi.json` | Modo singolo, a piedi | una sola rotta pedonale |
| `S07-modo-singolo-auto.json` | Modo singolo, in auto | una sola rotta in auto |
| `S08-modo-singolo-autobus.json` | Modo singolo, in autobus | un solo itinerario multimodale con i `legs` walk/ride separati (geometria per tratta e fermate) |
| `S09-origine-gps.json` | Origine dal GPS | `origin` = coordinate GPS, destinazione risolta da testo |
| `S10-citta-nominata.json` | Citta' nominata | estremi risolti con priorita' alla citta' indicata |
| `S11-follow-up.json` | Follow-up multi-turno | turno che riusa origine/destinazione dal contesto della conversazione |
| `S12-dest-categoria-supermercato.json` | Destinazione per categoria (supermercato) | destinazione = supermercato piu' vicino trovato per prossimita' |
| `S13-dest-categoria-farmacia.json` | Destinazione per categoria (farmacia) | idem con categoria farmacia |
| `S14-servizi-percorso-ristoranti.json` | Servizi lungo il percorso (ristoranti, auto) | rotta in auto con l'elenco dei ristoranti campionati lungo il tragitto in `routes[].services` |
| `S15-servizi-percorso-autobus.json` | Servizi lungo il percorso (in autobus) | itinerario bus con i servizi campionati attorno alle fermate e ai tratti a piedi |
| `S16-nearby-farmacie.json` | Servizi vicini (farmacie, da GPS) | `request_type` `nearby`: farmacie in `data.services` (top-level), nessuna rotta |
| `S17-nearby-centro-nominato.json` | Servizi vicini (centro nominato) | `nearby` centrato su un indirizzo/citta' indicato |
| `S18-nearby-parcheggi.json` | Servizi vicini (parcheggi) | `nearby` con categoria parcheggi |
| `S19-orario-partenza.json` | Orario di partenza | itinerario di trasporto pubblico con l'orario di partenza indicato |
| `S20-non-supportato.json` | Richiesta non supportata | payload con risposta cortese di funzione non supportata (nessun dato geografico) |
| `S21-chiarimento.json` (opzionale) | Chiarimento slot mancante | payload di richiesta di chiarimento, senza `data.services` (per non cancellare la mappa) |

## Formato

La forma del payload e' documentata in `README.md` → "The widget payload": `status`,
`request_type`, `data` (`wkt`, `distance_km`, `duration`, `mode`, `routes[]`, e per il bus
anche `legs[]`; per il tipo `nearby` invece `data.services`) e `messages[]` (l'ultimo
messaggio `assistant` e' il testo della risposta).

I file non contengono segreti: solo coordinate geografiche e nomi di servizi pubblici.
`job_id` e `stage` non compaiono perche' vivono solo nel livello di trasporto del bridge e
non entrano mai nel payload.
