"""
main.py - Akkuteszter Pico-oldali AUTOSTART belepesi pont.

Ez a fajl NEM a PC-n fut: az Akkuteszter GUI "Pico elokeszitese
BQ34Z110-hez" menupontja (Prog/src/pico_provisioning_service.py) tolti fel
a Raspberry Pi Pico MicroPython fajlrendszerenek GYOKERebe, a
`pico_bq_service.py` es a `pico_bq_rpc.py` melle.

MIERT `main.py`: a MicroPython minden bekapcsolaskor/reset-kor
automatikusan lefuttatja a flash gyokereben levo `main.py`-t (a `boot.py`
utan). Enelkul az RPC-szolgaltatas csak addig futna, amig a feltoltest
vegzo PC-oldali folyamat a raw-REPL kapcsolatot nyitva tartja - a
felhasznaloi elvaras viszont az, hogy a provisioning UTAN a normal
Akkuteszter GUI (Prog/drivers/pico_bq_transport.py :: PicoBQTransport) egy
UJ soros kapcsolattal barmikor racsatlakozhasson, es a szolgaltatas MAR
fusson.

MEGSZAKITHATOSAG (szandekos, nem mellekhatas): a `serve_forever()` a USB
CDC stdin-jerol olvas blokkolva. Egy Ctrl-C (0x03) KeyboardInterrupt-ot
valt ki benne, ami visszaadja a REPL-t - EZEN keresztul tud a provisioning
egy MAR ELOKESZITETT Pico-t ujra feltolteni/frissiteni. Ezert fut a hurok
`try/except KeyboardInterrupt` alatt, es ezert NEM indul ujra automatikusan
megszakitas utan.

HIBATURES: barmely MAS kivetel eseten a fuggveny visszater, a MicroPython
pedig kiirja a hibat es a REPL-hez esik vissza. Ez SZANDEKOS: egy hibas
allapotu eszkoz maradjon raw-REPL-lel elerheto (ujra provisionalhato),
soha ne legyen "beragadva" egy nema ujraindulasi hurokban.

EOF-KEZELES [HIL_PENDING]: a `serve_forever()` akkor is VISSZATER, ha a
`readline()` ures stringet (EOF) ad. A TF1-oldali kommentje szerint ez
valos Picon a gyakorlatban nem fordul elo (a USB CDC stream nem zar be),
DE ha megis igy lenne - pl. a PC lezarja a soros portot a provisioning
vegen -, akkor a szolgaltatas mar az elso portzaraskor leallna, es a
kovetkezo GUI-csatlakozas nem talalna futo szervert.
Ezert a hurkot itt ujraindito `while` fogja kozre. A tenyleges EOF-
viselkedes valos hardveren MEG NINCS igazolva (HIL_PENDING) - az
ujraindito hurok a biztonsagos irany: ha EOF sosem tortenik, a hurok
viselkedese azonos az egyszeru hivassal.
"""
# mypy: ignore-errors
# ruff: noqa
# (MicroPython forras - a PC-oldali statikus ellenorzok szamara adat.)

# Protokoll-verzio, amit a PC-oldali PicoBQTransport.negotiate() ellenoriz.
# Itt CSAK dokumentacios celt szolgal - az egyetlen mervado ertek a
# pico_bq_rpc.PROTOCOL_VERSION.
BOOT_BANNER = "akkuteszter-pico-bq-service"


def main():
    import time

    import pico_bq_service
    import pico_bq_rpc

    dispatcher = pico_bq_rpc.RpcDispatcher(
        pico_bq_rpc.build_methods(pico_bq_service)
    )
    while True:
        try:
            pico_bq_rpc.serve_forever(dispatcher)
        except KeyboardInterrupt:
            # Szandekos kilepes (Ctrl-C a provisioning-retegbol) - l. modul
            # fejlec "MEGSZAKITHATOSAG". A hurok itt VEGET er, hogy a
            # feltoltes/frissites at tudja venni a portot.
            return
        # Ide csak EOF-nal jutunk (l. fejlec "EOF-KEZELES"). Rovid
        # varakozas, hogy egy tartos EOF ne porgesse a CPU-t 100%-on; a
        # sleep megszakithato Ctrl-C-vel.
        time.sleep_ms(200)


main()
