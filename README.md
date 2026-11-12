# OpenVPN Panel

Dashboard web per gestire un server OpenVPN, con uno script che su **Proxmox VE** crea e configura
da solo il container LXC.

- **Utenti**: crea con un clic, QR e link di attivazione per il telefono, download `.ovpn`,
  disabilita/riabilita, rinnova il certificato (dispositivo perso), elimina.
- **Connessi in tempo reale**: IP VPN, indirizzo di provenienza, traffico, disconnessione forzata.
- **Server**: stato, versione, uptime, traffico, log, riavvio.
- **Impostazioni**: endpoint pubblico/DDNS, porta pubblica, tutto il traffico o solo alcune reti, DNS, client-to-client.

Il pannello parla con OpenVPN tramite la sua **interfaccia di management** (socket unix): legge i client
connessi, i log e lo stato, e disconnette i client senza toccare i file di OpenVPN.

## Installazione su Proxmox

1. Crea l'installer, che è un unico file con l'applicazione incorporata:
   ```bash
   python tools/build.py        # -> dist/ovpn-panel-proxmox.sh
   ```
2. Copialo sul nodo Proxmox (es. `scp dist/ovpn-panel-proxmox.sh root@proxmox:`) e dalla **Shell** del nodo:
   ```bash
   bash ovpn-panel-proxmox.sh
   ```
   Ti chiede il DDNS o l'IP pubblico, mostra il riepilogo e chiede conferma. Senza domande:
   ```bash
   bash ovpn-panel-proxmox.sh --yes --public-host casa.ddns.net
   ```
3. Alla fine stampa indirizzo del pannello, utente e password. La password resta anche nel container:
   `pct exec <ID> -- cat /root/ovpn-panel-credentials.txt`.
4. Sul router inoltra **1194/udp** verso l'IP del container.

Cosa fa lo script:
- sceglie il primo ID libero e lo storage `local-lvm`, se c'è;
- scarica l'ultimo template Debian 12;
- crea un container **unprivileged** (1 core, 512 MB di RAM, 4 GB di disco, DHCP su `vmbr0`, avvio automatico) e gli dà accesso a `/dev/net/tun`;
- dentro il container installa OpenVPN, la PKI (easy-rsa, chiavi EC, `tls-crypt`), il NAT e il pannello come servizi systemd.

Tutto si può cambiare con le opzioni, elencate da `bash ovpn-panel-proxmox.sh --help`:
`--ctid --hostname --storage --bridge --ip 192.168.1.50/24 --gw 192.168.1.1 --disk --memory --cores --port --proto tcp --panel-port --ssh-key`.

Se qualcosa va storto, il container resta in piedi per poterlo esaminare (`pct enter <ID>`).
Lo script stampa anche il comando per eliminarlo.

### Su un container o una VM Debian 12 già esistente

```bash
PUBLIC_HOST=casa.ddns.net bash app/install.sh
```

Rilanciarlo aggiorna l'applicazione, senza toccare PKI, utenti e impostazioni.

## Uso

**Panoramica → Nuovo utente**: inserisci il nome e compare il QR. L'utente lo inquadra con la fotocamera,
apre la pagina di attivazione, scarica il profilo e lo apre con **OpenVPN Connect**.
Il link vale 72 ore, ma se ne genera uno nuovo quando serve: il profilo si ricostruisce dalla PKI.

| Azione | Effetto |
|---|---|
| Disconnetti | chiude la sessione; il client si ricollega da solo |
| Disabilita | disconnette e blocca le riconnessioni, invalida i link |
| Rinnova | revoca il certificato e ne emette uno nuovo (dispositivo perso) |
| Elimina | revoca il certificato e cancella l'utente |

Impostazioni:
- **Indirizzo pubblico** (endpoint, porta, URL del pannello): vale per i profili scaricati da quel momento in poi.
- **Instradamento e DNS**: vale per tutti i client. Salvando, OpenVPN si riavvia e i client si riconnettono da soli.

Il pannello usa un certificato self-signed: browser e telefoni mostrano un avviso. Non serve esporlo su
Internet. Se vuoi che gli utenti aprano i QR da fuori casa, mettilo dietro un reverse proxy con un
certificato valido (es. Let's Encrypt sul DDNS) e indica quell'URL in Impostazioni.

### Comandi nel container

```bash
ovpn-panel reset-password admin                 # password dimenticata
journalctl -u ovpn-panel -u openvpn-server@server -f
```

## Sviluppo e test

`tests/run.sh` prova tutto senza un Proxmox vero:

1. lancia `dist/ovpn-panel-proxmox.sh` dentro un **nodo Proxmox simulato** (Debian 12 con systemd;
   `pct`, `pveam`, `pvesh` e `pvesm` finti, con `pct exec` che esegue davvero l'installer);
2. collega un **client OpenVPN reale** e prova creazione, attivazione, connessione, NAT, disconnessione,
   disabilitazione, rinnovo, impostazioni del server e riavvio;
3. riavvia il container e controlla che i servizi ripartano da soli.

```bash
bash tests/run.sh
```

Alla fine il pannello di prova resta su <https://localhost:9443>.

Il test non copre la parte specifica di Proxmox: comandi `pct` veri, template, storage e permessi LXC.
Quella va provata su un nodo reale.

## Struttura

```
app/
  install.sh            installazione dentro il container (Debian 12)
  ovpnpanel/
    main.py             API e pagine
    mgmt.py             client dell'interfaccia di management di OpenVPN
    pki.py              certificati (easy-rsa), revoche, profili .ovpn
    serverconf.py       server.conf generato dalle impostazioni
    settings.py         impostazioni modificabili dal pannello
    manage.py           CLI: init, create-admin, reset-password
    static/             pannello e pagina di attivazione
proxmox/create-ct.sh    script per il nodo Proxmox
tools/build.py          crea dist/ovpn-panel-proxmox.sh con l'app incorporata
tests/                  nodo Proxmox simulato e test end-to-end
```
