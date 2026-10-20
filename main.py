import os
import io
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session
import openpyxl
from personel_data import PERSONEL_DB

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local_test.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Bağlantı kararlılığı ve havuz (pool) yönetimi için eklenen parametreler
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
    guncelleme_zamani = Column(DateTime, default=datetime.utcnow)

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
    kayit_zamani = Column(DateTime, default=datetime.utcnow)

# --- TABLO 3: EXCEL'DEN GELEN MAKİNE-OPERASYON-MAMÜL-SİSTEM HIZI KATALOĞU ---
class HizKatalog(Base):
    __tablename__ = "hiz_katalogu"

    id = Column(Integer, primary_key=True, index=True)
    makine = Column(String(100), index=True)
    operasyon = Column(String(100), index=True)
    mamul_adi = Column(String(255))
    sistem_hizi = Column(String(50))

# --- TABLO 4: SAHADA GİRİLEN CANLI HAT HIZI KAYITLARI ---
class HatHiziKayit(Base):
    __tablename__ = "hat_hizi_kayitlari"

    id = Column(Integer, primary_key=True, index=True)
    makine = Column(String(100), index=True)
    operasyon = Column(String(100))
    mamul_adi = Column(String(255))
    sistem_hizi = Column(String(50))
    sahadaki_hiz = Column(String(50))
    aciklama = Column(String(255), nullable=True)
    kayit_zamani = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Özgüven Kablo - Üretim & Kontrol Sistemleri")
templates = Jinja2Templates(directory="templates")

PERSONEL_ISIMLERI = list(PERSONEL_DB.keys())
MAKINE_LISTESI = [
    "BAKIR 07", "BETA", "DOMEKS 1", "DOMEKS 2", "DRUM1", "DRUM2", "EFAF",
    "EXTRUDER 1", "EXTRUDER 10", "EXTRUDER 11", "EXTRUDER 2", "EXTRUDER 3",
    "EXTRUDER 4", "EXTRUDER 5", "EXTRUDER 6", "EXTRUDER 7", "EXTRUDER 8",
    "EXTRUDER 9", "HENRICH", "KULE1", "KULE2", "KULE3", "LESMO 1", "LESMO 2",
    "MULTISTRANDER 1-2", "MULTISTRANDER 3-4", "ÖZMAK-1", "PRATECH",
    "PLUSMAK BÜKÜM", "VEGA", "VATMAK", "ZIRHLAMA3"
]

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
        mevcut_kayit.guncelleme_zamani = datetime.utcnow()
    else:
        yeni_kayit = MesaiKayit(
            sicil_no=gercek_sicil, ad_soyad=ad_soyad, departman=gercek_personel["departman"],
            unvan=gercek_personel["unvan"], durum=durum, mazeret=mazeret
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
    bugun = datetime.now().strftime("%Y-%m-%d")
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
        durum=durum, aciklama=aciklama
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
# 3. MODÜL: CANLI HAT HIZI TAKİBİ (YENİ)
# ==========================================
@app.get("/hiz", response_class=HTMLResponse)
async def hiz_formu(request: Request, db: Session = Depends(get_db)):
    katalog_verisi = db.query(HizKatalog).all()
    katalog_list = [
        {
            "makine": k.makine,
            "operasyon": k.operasyon,
            "mamul": k.mamul_adi,
            "hiz": k.sistem_hizi
        }
        for k in katalog_verisi
    ]
    return templates.TemplateResponse(
        request=request, name="hiz.html",
        context={"mesaj": None, "katalog": katalog_list}
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
    sistem_hizi: str = Form(...),
    sahadaki_hiz: str = Form(...),
    aciklama: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    yeni_hiz = HatHiziKayit(
        makine=makine.strip(),
        operasyon=operasyon.strip(),
        mamul_adi=mamul_adi.strip(),
        sistem_hizi=sistem_hizi.strip(),
        sahadaki_hiz=sahadaki_hiz.strip(),
        aciklama=aciklama.strip() if aciklama else None
    )
    db.add(yeni_hiz)
    db.commit()

    katalog_verisi = db.query(HizKatalog).all()
    katalog_list = [
        {"makine": k.makine, "operasyon": k.operasyon, "mamul": k.mamul_adi, "hiz": k.sistem_hizi}
        for k in katalog_verisi
    ]
    bilgi = f"{makine} - {mamul_adi} için saha hızı ({sahadaki_hiz}) kaydedildi."
    return templates.TemplateResponse(
        request=request, name="hiz.html",
        context={"mesaj": bilgi, "hata": False, "katalog": katalog_list}
    )

@app.get("/admin/hiz", response_class=HTMLResponse)
async def admin_hiz_paneli(request: Request, db: Session = Depends(get_db), yukleme_mesaji: Optional[str] = None):
    kayitlar = db.query(HatHiziKayit).order_by(HatHiziKayit.kayit_zamani.desc()).all()
    katalog_sayisi = db.query(HizKatalog).count()
    return templates.TemplateResponse(
        request=request, name="admin_hiz.html",
        context={"kayitlar": kayitlar, "katalog_sayisi": katalog_sayisi, "yukleme_mesaji": yukleme_mesaji}
    )

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

        # Mevcut kataloğu temizle ve yenisini yaz
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

            # Pivot tablolarda üstteki hücre tekrar etmiyorsa (boşsa) bir önceki değeri koru (Forward Fill)
            if raw_makine is not None and str(raw_makine).strip() != "":
                son_makine = str(raw_makine).strip()
            if raw_operasyon is not None and str(raw_operasyon).strip() != "":
                son_operasyon = str(raw_operasyon).strip()

            # Genel Toplam / Total satırlarını atla
            if "toplam" in son_makine.lower() or "toplam" in son_operasyon.lower():
                continue

            if raw_mamul is not None and str(raw_mamul).strip() != "" and raw_hiz is not None:
                mamul_str = str(raw_mamul).strip()
                if "toplam" in mamul_str.lower():
                    continue
                
                # Sayısal hızları temiz formatla (Örn: 120.456 -> 120.5)
                try:
                    hiz_float = float(raw_hiz)
                    hiz_str = f"{hiz_float:.2f}".rstrip('0').rstrip('.')
                except ValueError:
                    hiz_str = str(raw_hiz).strip()

                if son_makine and son_operasyon and mamul_str:
                    db.add(HizKatalog(
                        makine=son_makine,
                        operasyon=son_operasyon,
                        mamul_adi=mamul_str,
                        sistem_hizi=hiz_str
                    ))
                    eklenen += 1

        db.commit()
        mesaj = f"Başarılı: Excel'den toplam {eklenen} adet kablo-hız eşleşmesi yüklendi."
    except Exception as e:
        mesaj = f"Hata oluştu: {str(e)}"

    kayitlar = db.query(HatHiziKayit).order_by(HatHiziKayit.kayit_zamani.desc()).all()
    katalog_sayisi = db.query(HizKatalog).count()
    return templates.TemplateResponse(
        request=request, name="admin_hiz.html",
        context={"kayitlar": kayitlar, "katalog_sayisi": katalog_sayisi, "yukleme_mesaji": mesaj}
    )
