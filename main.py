import os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from personel_data import PERSONEL_DB

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local_test.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
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

# --- TABLO 2: ACİL STOP VE SWITCH KONTROLÜ (YENİ) ---
class StopSwitchKayit(Base):
    __tablename__ = "stop_switch_kayitlari"

    id = Column(Integer, primary_key=True, index=True)
    tarih = Column(String(20))
    vardiya = Column(String(50))
    makine = Column(String(100))
    sicil_no = Column(String(50))
    ad_soyad = Column(String(100))
    acil_stop = Column(Boolean, default=False)
    switch = Column(Boolean, default=False)
    durum = Column(String(20))
    aciklama = Column(String(255), nullable=True)
    kayit_zamani = Column(DateTime, default=datetime.utcnow)

# Veritabanını güncelle/oluştur
# Geliştirme aşamasındaki tablo çakışmalarını önlemek için checkfirst=True ile oluşturuyoruz
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
# 1. MODÜL: PAZAR MESAİSİ
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"mesaj": None, "personeller": PERSONEL_ISIMLERI}
    )

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
        return templates.TemplateResponse(request=request, name="index.html", context={"mesaj": "Personel bulunamadı.", "hata": True, "personeller": PERSONEL_ISIMLERI})
    
    gercek_personel = PERSONEL_DB[ad_soyad]
    
    if gercek_personel["sicil_no"] != girilen_sicil:
        return templates.TemplateResponse(request=request, name="index.html", context={"mesaj": "Hatalı Sicil Numarası.", "hata": True, "personeller": PERSONEL_ISIMLERI})
        
    if durum == "GELMİYOR" and (not mazeret or not mazeret.strip()):
        return templates.TemplateResponse(request=request, name="index.html", context={"mesaj": "Mazeret zorunludur.", "hata": True, "personeller": PERSONEL_ISIMLERI})

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
    
    return templates.TemplateResponse(request=request, name="index.html", context={"mesaj": f"Başarılı: {durum}", "hata": False, "durum": durum, "personeller": PERSONEL_ISIMLERI})

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
        request=request, 
        name="kontrol.html", 
        context={"mesaj": None, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": bugun}
    )

@app.post("/kontrol/bildir", response_class=HTMLResponse)
async def kontrol_kaydet(
    request: Request,
    tarih: str = Form(...),
    vardiya: str = Form(...),
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
    
    # Personel Doğrulama
    if ad_soyad not in PERSONEL_DB or PERSONEL_DB[ad_soyad]["sicil_no"] != girilen_sicil:
        return templates.TemplateResponse(
            request=request, name="kontrol.html", 
            context={"mesaj": "Hatalı Sicil Numarası veya Personel.", "hata": True, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": tarih}
        )
    
    # Uygun değilse açıklama zorunluluğu
    if durum == "Uygun Değil" and (not aciklama or not aciklama.strip()):
         return templates.TemplateResponse(
            request=request, name="kontrol.html", 
            context={"mesaj": "Uygunsuzluk durumunda açıklama yazmak zorunludur.", "hata": True, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": tarih}
        )

    # Boolean dönüşümleri
    is_acil_stop = acil_stop_val == "1"
    is_switch = switch_val == "1"
    
    # Mesai formunun aksine, bu bir LOG (Kayıt) tablosudur. Üzerine yazmaz (Upsert yapmaz), alt alta ekler.
    yeni_kayit = StopSwitchKayit(
        tarih=tarih,
        vardiya=vardiya,
        makine=makine,
        sicil_no=PERSONEL_DB[ad_soyad]["sicil_no"],
        ad_soyad=ad_soyad,
        acil_stop=is_acil_stop,
        switch=is_switch,
        durum=durum,
        aciklama=aciklama
    )
    
    db.add(yeni_kayit)
    db.commit()
    
    bilgi = f"{makine} kontrolünüz başarıyla kaydedildi."
    return templates.TemplateResponse(
        request=request, name="kontrol.html", 
        context={"mesaj": bilgi, "hata": False, "personeller": PERSONEL_ISIMLERI, "makineler": MAKINE_LISTESI, "bugun": tarih}
    )

@app.get("/admin/kontrol", response_class=HTMLResponse)
async def admin_kontrol_paneli(request: Request, db: Session = Depends(get_db)):
    # En son kaydedilen en üstte görünsün
    kayitlar = db.query(StopSwitchKayit).order_by(StopSwitchKayit.kayit_zamani.desc()).all()
    return templates.TemplateResponse(
        request=request, name="admin_kontrol.html", 
        context={"kayitlar": kayitlar}
    )
