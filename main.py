import os
import io
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from fastapi import FastAPI, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
import openpyxl
from personel_data import PERSONEL_DB

# --- ZAMAN DİLİMİ FONKSİYONU (UTC+3 TÜRKİYE SAATİ) ---
def get_turkey_time():
    return datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)

def tr_upper(metin: str) -> str:
    if not metin:
        return ""
    return str(metin).replace("i", "İ").replace("ı", "I").upper()

def normalize_key(metin: str) -> str:
    if not metin:
        return ""
    return " ".join(tr_upper(metin).split())

def parse_speed_str(raw_val) -> str:
    if raw_val is None:
        return ""
    if isinstance(raw_val, (int, float)):
        val = float(raw_val)
    else:
        temiz = str(raw_val).strip().replace(",", ".").rstrip(".")
        if not temiz:
            return ""
        try:
            val = float(temiz)
        except ValueError:
            return ""
    if val <= 0:
        return ""
    return f"{val:.2f}".rstrip("0").rstrip(".")

def to_float_safe(val) -> Optional[float]:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(str(val).strip().replace(",", ".").rstrip("."))
    except (ValueError, TypeError):
        return None

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local_test.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- TABLO 1: PAZAR MESAİSİ ---
class MesaiKayit(Base):
    __tablename__ = "mesai_kayitlari"

    id = Column(Integer, primary_key=True, index=True)
    sicil_no = Column(String(50), index=True)
    ad_soyad = Column(String(100))
    departman = Column(String(100))
    unvan = Column(String(100))
    durum = Column(String(20))
    mazeret = Column(String(255), nullable=True)
    guncelleme_zamani = Column(DateTime, default=get_turkey_time)

# --- TABLO 2: ACİL STOP VE SWITCH KONTROLÜ ---
class StopSwitchKayit(Base):
    __tablename__ = "stop_switch_kayitlari"

    id = Column(Integer, primary_key=True, index=True)
    tarih = Column(String(20))
    makine = Column(String(100))
    sicil_no = Column(String(50))
    ad_soyad = Column(String(100))
    acil_stop = Column(Boolean, default=False)
    switch = Column(Boolean, default=False)
    durum = Column(String(20))
    aciklama = Column(String(255), nullable=True)
    kayit_zamani = Column(DateTime, default=get_turkey_time)

# --- TABLO 3: HIZ KATALOĞU (2 YILLIK ORT. + ERP ROTA HIZI) ---
class HizKatalog(Base):
    __tablename__ = "hiz_katalogu"

    id = Column(Integer, primary_key=True, index=True)
    makine = Column(String(100), index=True)
    operasyon = Column(String(100), index=True)
    mamul_adi = Column(String(255))
    sistem_hizi = Column(String(50), nullable=True)   # 2 Yıllık Ort. Hız
    erp_hizi = Column(String(50), nullable=True)      # ERP Rota Hızı

# --- TABLO 4: SAHADA GİRİLEN CANLI HAT HIZI KAYITLARI ---
class HatHiziKayit(Base):
    __tablename__ = "hat_hizi_kayitlari"

    id = Column(Integer, primary_key=True, index=True)
    makine = Column(String(100), index=True)
    operasyon = Column(String(100))
    mamul_adi = Column(String(255))
    sistem_hizi = Column(String(50), nullable=True)   # 2 Yıllık Ort. Hız
    erp_hizi = Column(String(50), nullable=True)      # ERP Rota Hızı
    sahadaki_hiz = Column(String(50))
    aciklama = Column(String(255), nullable=True)
    kayit_zamani = Column(DateTime, default=get_turkey_time)

Base.metadata.create_all(bind=engine)

# Otomatik Sütun ve Sequence Güncellemesi
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE hiz_katalogu ADD COLUMN IF NOT EXISTS erp_hizi VARCHAR(50);"))
        conn.execute(text("ALTER TABLE hat_hizi_kayitlari ADD COLUMN IF NOT EXISTS erp_hizi VARCHAR(50);"))
        conn.commit()
    except Exception as e:
        print(f"Column migration info: {e}")

    if "postgresql" in DATABASE_URL:
        try:
            tablolar = ["hat_hizi_kayitlari", "hiz_katalogu", "mesai_kayitlari", "stop_switch_kayitlari"]
            for tbl in tablolar:
                conn.execute(text(f"""
                    SELECT setval(
                        pg_get_serial_sequence('{tbl}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {tbl}), 0) + 1,
                        false
                    );
                """))
            conn.commit()
        except Exception as e:
            print(f"Sequence sync bypassed: {e}")

app = FastAPI(title="Özgüven Kablo - Üretim & Kontrol Sistemleri")
templates = Jinja2Templates(directory="templates")

from kombi.db import init_schema as init_kombi_schema
from kombi.web import router as kombi_router

if os.getenv("KOMBI_DATABASE_URL") or "postgresql" in DATABASE_URL:
    try:
        init_kombi_schema()
    except Exception as e:
        print(f"Kombi schema init bypassed: {e}")

app.include_router(kombi_router)

PERSONEL_ISIMLERI = list(PERSONEL_DB.keys())
MAKINE_LISTESI = [
    "BAKIR 07", "BETA", "DOMEKS 1", "DOMEKS 2", "DRUM1", "DRUM2", "EFAF",
    "EXTRUDER 1", "EXTRUDER 10", "EXTRUDER 11", "EXTRUDER 2", "EXTRUDER 3",
    "EXTRUDER 4", "EXTRUDER 5", "EXTRUDER 6", "EXTRUDER 7", "EXTRUDER 8",
    "EXTRUDER 9", "HENRICH", "KULE1", "KULE2", "KULE3", "LESMO 1", "LESMO 2",
    "MULTISTRANDER 1-2", "MULTISTRANDER 3-4", "ÖZMAK-1", "PRATECH",
    "PLUSMAK BÜKÜM", "VEGA", "VATMAK", "ZIRHLAMA3"
]

FALLBACK_MAKINE_MAP = {
    "EXT1": "(EXT1) EXTRUDER 1",
    "EXT2": "(EXT2) EXTRUDER 2",
    "EXT3": "(EXT3) EXTRUDER 3",
    "EXT4": "(EXT4) EXTRUDER 4",
    "EXT5": "(EXT5) EXTRUDER 5",
    "EXT6": "(EXT6) EXTRUDER 6",
    "EXT7": "(EXT7) EXTRUDER 7",
    "EXT8": "(EXT8) EXTRUDER 8",
    "EXT9": "(EXT9) EXTRUDER 9",
    "EXT10": "(EXT10) EXTRUDER 10",
    "EXT11": "(EXT11) EXTRUDER 11",
    "DRM1": "(DRM1) DRUM1",
    "DRM2": "(DRM2) DRUM2",
    "MST1": "(MST1) MULTISTRANDER-1",
    "MST2": "(MST2) MULTISTRANDER-2",
    "MST4": "(MST4) MULTISTRANDER-4",
    "ZRH3": "(ZRH3) ZIRHLAMA3",
    "OMB1": "(OMB1) ÖZMAK-1",
    "VTB": "(VTB) VATMAK",
}

def resolve_machine_name(dosya_adi: str, mevcut_makineler: list) -> str:
    kod = tr_upper(os.path.splitext(dosya_adi)[0]).strip()
    hedef_parantez = f"({kod})"
    for m in mevcut_makineler:
        if hedef_parantez in tr_upper(m):
            return m
    for m in mevcut_makineler:
        if tr_upper(m).strip() == kod:
            return m
    if kod in FALLBACK_MAKINE_MAP:
        return FALLBACK_MAKINE_MAP[kod]
    return kod

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 0. ANA MENÜ
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def ana_menu(request: Request):
    return templates.TemplateResponse(request=request, name="menu.html", context={})

# ==========================================
# 1. MODÜL: PAZAR MESAİSİ
# ==========================================
@app.get("/mesai", response_class=HTMLResponse)
async def mesai_formu(request: Request):
    return templates.TemplateResponse(
        request=request, name="mesai.html", context={"mesaj": None, "personeller": PERSONEL_ISIMLERI}
    )

@app.get("/bildir", response_class=RedirectResponse)
async def bildirim_yanlis_giris():
    return RedirectResponse(url="/mesai", status_code=303)

@app.post("/bildir", response_class=HTMLResponse)
async def bildirim_yap(
    request: Request,
    ad_soyad: str = Form(...),
    girilen_sicil: str = Form(...),
    durum: str = Form(...),
    mazeret: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    ad_soyad = ad_soyad.strip()
    girilen_sicil = girilen_sicil.strip()
    
    if ad_soyad not in PERSONEL_DB:
        return templates.TemplateResponse(request=request, name="mesai.html", context={"mesaj": "Personel bulunamadı.", "hata": True, "personeller": PERSONEL_ISIMLERI})
    
    gercek_personel = PERSONEL_DB[ad_soyad]
    if gercek_personel["sicil_no"] != girilen_sicil:
        return templates.TemplateResponse(request=request, name="mesai.html", context={"mesaj": "Hatalı Sicil Numarası.", "hata": True, "personeller": PERSONEL_ISIMLERI})
        
    if durum == "GELMİYOR" and (not mazeret or not mazeret.strip()):
        return templates.TemplateResponse(request=request, name="mesai.html", context={"mesaj": "Mazeret zorunludur.", "hata": True, "personeller": PERSONEL_ISIMLERI})

    gercek_sicil = gercek_personel["sicil_no"]
    mevcut_kayit = db.query(MesaiKayit).filter(MesaiKayit.sicil_no == gercek_sicil).first()
    
    if mevcut_kayit:
        mevcut_kayit.ad_soyad = ad_soyad
        mevcut_kayit.departman = gercek_personel["departman"]
        mevcut_kayit.unvan = gercek_personel["unvan"]
        mevcut_kayit.durum = durum
        mevcut_kayit.mazeret = mazeret
        mevcut_kayit.guncelleme_zamani = get_turkey_time()
    else:
        yeni_kayit = MesaiKayit(
            sicil_no=gercek_sicil, ad_soyad=ad_soyad, departman=gercek_personel["departman"],
            unvan=gercek_personel["unvan"], durum=durum, mazeret=mazeret,
            guncelleme_zamani=get_turkey_time()
        )
        db.add(yeni_kayit)
    
    db.commit()
    return templates.TemplateResponse(request=request, name="mesai.html", context={"mesaj": f"Başarılı: {durum}", "hata": False, "durum": durum, "personeller": PERSONEL_ISIMLERI})

@app.get("/admin", response_class=HTMLResponse)
async def admin_paneli(request: Request, db: Session = Depends(get_db)):
    kayitlar = db.query(MesaiKayit).all()
    return templates.TemplateResponse(
        request=request, name="admin.html", 
        context={"kayitlar": kayitlar, "gelen": sum(1 for k in kayitlar if k.durum == "GELİYOR"), "gelmeyen": sum(1 for k in kayitlar if k.durum == "GELMİYOR"), "toplam": len(kayitlar)}
    )

@app.post("/admin/sifirla")
async def kayitlari_sifirla(db: Session = Depends(get_db)):
    db.query(MesaiKayit).delete()
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)

# ==========================================
# 2. MODÜL: ACİL STOP / SWITCH KONTROLÜ
# ==========================================
@app.get("/kontrol", response_class=HTMLResponse)
async def kontrol_formu(request: Request):
    bugun = get_turkey_time().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request=request, name="kontrol.html", 
        context={"mesaj": None, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": bugun}
    )

@app.get("/kontrol/bildir", response_class=RedirectResponse)
async def kontrol_yanlis_giris():
    return RedirectResponse(url="/kontrol", status_code=303)

@app.post("/kontrol/bildir", response_class=HTMLResponse)
async def kontrol_kaydet(
    request: Request,
    tarih: str = Form(...),
    makine: str = Form(...),
    ad_soyad: str = Form(...),
    girilen_sicil: str = Form(...),
    acil_stop_val: str = Form(...),
    switch_val: str = Form(...),
    durum: str = Form(...),
    aciklama: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    ad_soyad = ad_soyad.strip()
    girilen_sicil = girilen_sicil.strip()
    
    if ad_soyad not in PERSONEL_DB or PERSONEL_DB[ad_soyad]["sicil_no"] != girilen_sicil:
        return templates.TemplateResponse(
            request=request, name="kontrol.html", 
            context={"mesaj": "Hatalı Sicil Numarası veya Personel.", "hata": True, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": tarih}
        )
    
    if durum == "Uygun Değil" and (not aciklama or not aciklama.strip()):
         return templates.TemplateResponse(
            request=request, name="kontrol.html", 
            context={"mesaj": "Uygunsuzluk durumunda açıklama yazmak zorunludur.", "hata": True, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": tarih}
        )

    yeni_kayit = StopSwitchKayit(
        tarih=tarih, makine=makine, sicil_no=PERSONEL_DB[ad_soyad]["sicil_no"],
        ad_soyad=ad_soyad, acil_stop=(acil_stop_val == "1"), switch=(switch_val == "1"),
        durum=durum, aciklama=aciklama,
        kayit_zamani=get_turkey_time()
    )
    db.add(yeni_kayit)
    db.commit()
    
    return templates.TemplateResponse(
        request=request, name="kontrol.html", 
        context={"mesaj": f"{makine} kontrolünüz başarıyla kaydedildi.", "hata": False, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": tarih}
    )

@app.get("/admin/kontrol", response_class=HTMLResponse)
async def admin_kontrol_paneli(request: Request, db: Session = Depends(get_db)):
    kayitlar = db.query(StopSwitchKayit).order_by(StopSwitchKayit.kayit_zamani.desc()).all()
    return templates.TemplateResponse(request=request, name="admin_kontrol.html", context={"kayitlar": kayitlar})

# ==========================================
# 3. MODÜL: CANLI HAT HIZI TAKİBİ (2 YILLIK + ERP ROTA HIZI)
# ==========================================
def get_katalog_json(db: Session):
    katalog_verisi = db.query(HizKatalog).all()
    return [
        {
            "makine": k.makine or "",
            "operasyon": k.operasyon or "",
            "mamul": k.mamul_adi or "",
            "hiz": k.sistem_hizi or "",
            "erp_hiz": k.erp_hizi or ""
        }
        for k in katalog_verisi
    ]

@app.get("/hiz", response_class=HTMLResponse)
async def hiz_formu(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request, name="hiz.html",
        context={"mesaj": None, "katalog": get_katalog_json(db)}
    )

@app.get("/hiz/bildir", response_class=RedirectResponse)
async def hiz_yanlis_giris():
    return RedirectResponse(url="/hiz", status_code=303)

@app.post("/hiz/bildir", response_class=HTMLResponse)
async def hiz_kaydet(
    request: Request,
    makine: str = Form(...),
    operasyon: str = Form(...),
    mamul_adi: str = Form(...),
    sistem_hizi: Optional[str] = Form(""),
    erp_hizi: Optional[str] = Form(""),
    sahadaki_hiz: str = Form(...),
    aciklama: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    temiz_sahadaki = sahadaki_hiz.strip().replace(",", ".")
    yeni_hiz = HatHiziKayit(
        makine=makine.strip(),
        operasyon=operasyon.strip(),
        mamul_adi=mamul_adi.strip(),
        sistem_hizi=sistem_hizi.strip() if sistem_hizi else "",
        erp_hizi=erp_hizi.strip() if erp_hizi else "",
        sahadaki_hiz=temiz_sahadaki,
        aciklama=aciklama.strip() if aciklama else None,
        kayit_zamani=get_turkey_time()
    )
    db.add(yeni_hiz)
    db.commit()

    bilgi = f"{makine} - {mamul_adi} için saha hızı ({temiz_sahadaki}) kaydedildi."
    return templates.TemplateResponse(
        request=request, name="hiz.html",
        context={"mesaj": bilgi, "hata": False, "katalog": get_katalog_json(db)}
    )

def build_admin_hiz_context(request: Request, db: Session, yukleme_mesaji: Optional[str] = None):
    ham_kayitlar = db.query(HatHiziKayit).order_by(HatHiziKayit.kayit_zamani.desc()).all()
    katalog_sayisi = db.query(HizKatalog).count()
    erp_sayisi = db.query(HizKatalog).filter(HizKatalog.erp_hizi != None, HizKatalog.erp_hizi != "").count()

    kayitlar = []
    for k in ham_kayitlar:
        erp_f = to_float_safe(k.erp_hizi)
        saha_f = to_float_safe(k.sahadaki_hiz)
        fark_yuzde = None
        if erp_f and erp_f > 0 and saha_f is not None:
            fark_yuzde = round(((saha_f - erp_f) / erp_f) * 100, 1)

        kayitlar.append({
            "id": k.id,
            "kayit_zamani": k.kayit_zamani,
            "makine": k.makine,
            "operasyon": k.operasyon,
            "mamul_adi": k.mamul_adi,
            "sistem_hizi": k.sistem_hizi,
            "erp_hizi": k.erp_hizi,
            "sahadaki_hiz": k.sahadaki_hiz,
            "fark_yuzde": fark_yuzde,
            "aciklama": k.aciklama
        })

    return {
        "kayitlar": kayitlar,
        "katalog_sayisi": katalog_sayisi,
        "erp_sayisi": erp_sayisi,
        "yukleme_mesaji": yukleme_mesaji
    }

@app.get("/admin/hiz", response_class=HTMLResponse)
async def admin_hiz_paneli(request: Request, db: Session = Depends(get_db), yukleme_mesaji: Optional[str] = None):
    return templates.TemplateResponse(
        request=request, name="admin_hiz.html",
        context=build_admin_hiz_context(request, db, yukleme_mesaji)
    )

# --- 2 YILLIK PİVOT EXCEL YÜKLEME ---
@app.post("/admin/hiz/excel-yukle", response_class=HTMLResponse)
async def excel_katalog_yukle(
    request: Request,
    excel_dosyasi: UploadFile = File(...),
    makine_sutun: int = Form(1),
    operasyon_sutun: int = Form(2),
    mamul_sutun: int = Form(3),
    hiz_sutun: int = Form(4),
    baslangic_satiri: int = Form(2),
    db: Session = Depends(get_db)
):
    try:
        icerik = await excel_dosyasi.read()
        wb = openpyxl.load_workbook(io.BytesIO(icerik), data_only=True)
        ws = wb.active

        # Mevcut ERP hızlarını hafızaya al ki 2 yıllık güncelleme ERP hızlarını silmesin
        eski_erp_map = {}
        for k in db.query(HizKatalog).filter(HizKatalog.erp_hizi != None, HizKatalog.erp_hizi != "").all():
            anahtar = (k.makine, normalize_key(k.operasyon), normalize_key(k.mamul_adi))
            eski_erp_map[anahtar] = (k.operasyon, k.mamul_adi, k.erp_hizi)

        db.query(HizKatalog).delete()

        son_makine = ""
        son_operasyon = ""
        eklenen = 0

        for row in ws.iter_rows(min_row=baslangic_satiri, values_only=True):
            if not row or len(row) < max(makine_sutun, operasyon_sutun, mamul_sutun, hiz_sutun):
                continue

            raw_makine = row[makine_sutun - 1]
            raw_operasyon = row[operasyon_sutun - 1]
            raw_mamul = row[mamul_sutun - 1]
            raw_hiz = row[hiz_sutun - 1]

            if raw_makine is not None and str(raw_makine).strip() != "":
                son_makine = str(raw_makine).strip()
            if raw_operasyon is not None and str(raw_operasyon).strip() != "":
                son_operasyon = str(raw_operasyon).strip()

            if "toplam" in son_makine.lower() or "toplam" in son_operasyon.lower():
                continue

            if raw_mamul is not None and str(raw_mamul).strip() != "" and raw_hiz is not None:
                mamul_str = str(raw_mamul).strip()
                if "toplam" in mamul_str.lower():
                    continue
                
                hiz_str = parse_speed_str(raw_hiz) or str(raw_hiz).strip()

                if son_makine and son_operasyon and mamul_str:
                    anahtar = (son_makine, normalize_key(son_operasyon), normalize_key(mamul_str))
                    eslesmis_erp = eski_erp_map.pop(anahtar, None)
                    db.add(HizKatalog(
                        makine=son_makine,
                        operasyon=son_operasyon,
                        mamul_adi=mamul_str,
                        sistem_hizi=hiz_str,
                        erp_hizi=eslesmis_erp[2] if eslesmis_erp else None
                    ))
                    eklenen += 1

        # Sadece ERP'de olup 2 yıllık pivotta olmayan kayıtları geri ekle
        for (makine_ad, _, _), (op_ad, mamul_ad, erp_val) in eski_erp_map.items():
            db.add(HizKatalog(
                makine=makine_ad,
                operasyon=op_ad,
                mamul_adi=mamul_ad,
                sistem_hizi="",
                erp_hizi=erp_val
            ))

        db.commit()
        mesaj = f"Başarılı: 2 yıllık katalogdan {eklenen} adet hız eşleşmesi güncellendi."
    except Exception as e:
        db.rollback()
        mesaj = f"Hata oluştu: {str(e)}"

    return templates.TemplateResponse(
        request=request, name="admin_hiz.html",
        context=build_admin_hiz_context(request, db, mesaj)
    )

# --- 11 ADET ERP EXCEL DOSYASI TOPLU YÜKLEME (DOSYA ADI = MAKİNE KODU) ---
@app.post("/admin/hiz/erp-yukle", response_class=HTMLResponse)
async def erp_katalog_yukle(
    request: Request,
    erp_dosyalari: List[UploadFile] = File(...),
    operasyon_sutun: int = Form(6),   # F Sütunu: Operasyon Adı
    mamul_sutun: int = Form(14),      # N Sütunu: Mamül Adı
    hiz_sutun: int = Form(11),        # K Sütunu: Hız(Birim/Dk)
    baslangic_satiri: int = Form(2),
    db: Session = Depends(get_db)
):
    try:
        # 1. Eski ERP hızlarını sıfırla (2 yıllık verisi olmayan sadece-ERP satırlarını temizle)
        db.query(HizKatalog).filter(
            (HizKatalog.sistem_hizi == None) | (HizKatalog.sistem_hizi == "")
        ).delete(synchronize_session=False)
        db.query(HizKatalog).update({HizKatalog.erp_hizi: None}, synchronize_session=False)
        db.flush()

        mevcut_kayitlar = db.query(HizKatalog).all()
        mevcut_makineler = sorted(list({k.makine for k in mevcut_kayitlar if k.makine}))

        # Mevcut kayıtları hızlı eşleştirme sözlüğüne al: (makine, norm_op, norm_mamul) -> HizKatalog objesi
        katalog_map = {}
        for k in mevcut_kayitlar:
            anahtar = (k.makine, normalize_key(k.operasyon), normalize_key(k.mamul_adi))
            katalog_map[anahtar] = k

        toplam_dosya = 0
        eslesen_satir = 0
        yeni_eklenen_satir = 0

        for dosya in erp_dosyalari:
            if not dosya.filename or not dosya.filename.lower().endswith((".xlsx", ".xlsm")):
                continue

            toplam_dosya += 1
            makine_adi = resolve_machine_name(dosya.filename, mevcut_makineler)
            icerik = await dosya.read()
            wb = openpyxl.load_workbook(io.BytesIO(icerik), data_only=True)
            ws = wb.active

            # Dosya içindeki satırları oku ("İşaretli" varsayılan rotaya öncelik ver)
            dosya_rotalari = {}
            for row in ws.iter_rows(min_row=baslangic_satiri, values_only=True):
                if not row or len(row) < max(operasyon_sutun, mamul_sutun, hiz_sutun):
                    continue

                raw_vars = row[0] if len(row) > 0 else None
                raw_op = row[operasyon_sutun - 1]
                raw_mamul = row[mamul_sutun - 1]
                raw_hiz = row[hiz_sutun - 1]

                if not raw_op or not raw_mamul or raw_hiz is None:
                    continue

                op_str = str(raw_op).strip()
                mamul_str = str(raw_mamul).strip()
                hiz_str = parse_speed_str(raw_hiz)

                if not op_str or not mamul_str or not hiz_str:
                    continue

                is_default = (tr_upper(str(raw_vars).strip()) == "İŞARETLİ") if raw_vars else False
                norm_anahtar = (normalize_key(op_str), normalize_key(mamul_str))

                if norm_anahtar not in dosya_rotalari or (is_default and not dosya_rotalari[norm_anahtar]["is_default"]):
                    dosya_rotalari[norm_anahtar] = {
                        "operasyon": op_str,
                        "mamul": mamul_str,
                        "hiz": hiz_str,
                        "is_default": is_default
                    }

            # Veritabanıyla eşleştir veya yeni ekle
            for (norm_op, norm_mamul), veri in dosya_rotalari.items():
                db_anahtar = (makine_adi, norm_op, norm_mamul)
                if db_anahtar in katalog_map:
                    katalog_map[db_anahtar].erp_hizi = veri["hiz"]
                    eslesen_satir += 1
                else:
                    yeni_kayit = HizKatalog(
                        makine=makine_adi,
                        operasyon=veri["operasyon"],
                        mamul_adi=veri["mamul"],
                        sistem_hizi="",
                        erp_hizi=veri["hiz"]
                    )
                    db.add(yeni_kayit)
                    katalog_map[db_anahtar] = yeni_kayit
                    yeni_eklenen_satir += 1

        db.commit()
        mesaj = (
            f"Başarılı: {toplam_dosya} adet ERP dosyası işlendi. "
            f"{eslesen_satir} mevcut ürün güncellendi, {yeni_eklenen_satir} yeni ERP rotası eklendi."
        )
    except Exception as e:
        db.rollback()
        mesaj = f"ERP Yükleme Hatası: {str(e)}"

    return templates.TemplateResponse(
        request=request, name="admin_hiz.html",
        context=build_admin_hiz_context(request, db, mesaj)
    )

# --- EXCEL RAPORU İNDİRME (SAYI VE TARİH FORMATLI) ---
@app.get("/admin/hiz/excel-indir")
def admin_hiz_excel_indir(db: Session = Depends(get_db)):
    kayitlar = db.query(HatHiziKayit).order_by(HatHiziKayit.id.desc()).all()
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Hat Hızı Kayıtları"
    
    headers = [
        "Kayıt ID", "Kayıt Zamanı", "Makine", "Operasyon",
        "Mamül (Kablo) Adı", "ERP Rota Hızı", "2 Yıllık Ort. Hız",
        "Sahadaki Hız", "ERP Fark (%)", "Açıklama"
    ]
    ws.append(headers)
    
    def sayiya_cevir(val):
        f = to_float_safe(val)
        if f is None:
            return ""
        return int(f) if f.is_integer() else f

    for k in kayitlar:
        zaman_degeri = k.kayit_zamani.replace(tzinfo=None) if k.kayit_zamani else ""
        erp_f = to_float_safe(k.erp_hizi)
        saha_f = to_float_safe(k.sahadaki_hiz)
        fark_val = round(((saha_f - erp_f) / erp_f) * 100, 1) if (erp_f and erp_f > 0 and saha_f is not None) else ""

        satir = [
            k.id,
            zaman_degeri,
            str(k.makine).strip() if k.makine else "",
            str(k.operasyon).strip() if k.operasyon else "",
            str(k.mamul_adi).strip() if k.mamul_adi else "",
            sayiya_cevir(k.erp_hizi),
            sayiya_cevir(k.sistem_hizi),
            sayiya_cevir(k.sahadaki_hiz),
            fark_val,
            str(k.aciklama).strip() if k.aciklama else ""
        ]
        ws.append(satir)

        if zaman_degeri != "":
            ws.cell(row=ws.max_row, column=2).number_format = "DD.MM.YYYY HH:MM"

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    
    dosya_adi = f"hat_hizi_kayitlari_{get_turkey_time().strftime('%d_%m_%Y')}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={dosya_adi}"}
    )
