import os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from personel_data import PERSONEL_DB

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local_test.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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

# Eğer eski tablo varsa önce siler, sonra yeni (departman ve unvan eklenmiş) haliyle sıfırdan oluşturur
MesaiKayit.__table__.drop(engine, checkfirst=True)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Pazar Mesaisi Bildirim Sistemi")
templates = Jinja2Templates(directory="templates")

# İsim listesini alfabetik olarak şablona göndermek için
PERSONEL_ISIMLERI = list(PERSONEL_DB.keys())

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"mesaj": None, "personeller": PERSONEL_ISIMLERI}
    )
@app.get("/bildir", response_class=RedirectResponse)
async def bildirim_yanlis_giris():
    return RedirectResponse(url="/", status_code=303)
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
    
    # 1. Personel listede var mı?
    if ad_soyad not in PERSONEL_DB:
        hata = "Seçilen personel sistemde bulunamadı."
        return templates.TemplateResponse(
            request=request, 
            name="index.html", 
            context={"mesaj": hata, "hata": True, "personeller": PERSONEL_ISIMLERI}
        )
    
    gercek_personel = PERSONEL_DB[ad_soyad]
    
    # 2. Sicil Numarası (PIN) eşleşiyor mu?
    if gercek_personel["sicil_no"] != girilen_sicil:
        hata = "Hatalı Sicil Numarası girdiniz. Lütfen tekrar deneyin."
        return templates.TemplateResponse(
            request=request, 
            name="index.html", 
            context={"mesaj": hata, "hata": True, "personeller": PERSONEL_ISIMLERI}
        )
        
    # 3. Mazeret kontrolü (Backend doğrulaması)
    if durum == "GELMİYOR" and (not mazeret or not mazeret.strip()):
        hata = "Gelmeyeceğinizi belirttiniz. Lütfen mazeretinizi yazınız."
        return templates.TemplateResponse(
            request=request, 
            name="index.html", 
            context={"mesaj": hata, "hata": True, "personeller": PERSONEL_ISIMLERI}
        )

    # Doğrulama başarılı, kaydı UPSERT yap (Varsa güncelle, yoksa ekle)
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
            sicil_no=gercek_sicil,
            ad_soyad=ad_soyad,
            departman=gercek_personel["departman"],
            unvan=gercek_personel["unvan"],
            durum=durum,
            mazeret=mazeret
        )
        db.add(yeni_kayit)
    
    db.commit()
    
    bilgi = f"İşlem Başarılı. Kaydınız oluşturuldu: {durum}"
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"mesaj": bilgi, "hata": False, "durum": durum, "personeller": PERSONEL_ISIMLERI}
    )

@app.get("/admin", response_class=HTMLResponse)
async def admin_paneli(request: Request, db: Session = Depends(get_db)):
    kayitlar = db.query(MesaiKayit).all()
    gelen_sayisi = sum(1 for k in kayitlar if k.durum == "GELİYOR")
    gelmeyen_sayisi = sum(1 for k in kayitlar if k.durum == "GELMİYOR")
    
    return templates.TemplateResponse(
        request=request, 
        name="admin.html", 
        context={
            "kayitlar": kayitlar,
            "gelen": gelen_sayisi,
            "gelmeyen": gelmeyen_sayisi,
            "toplam": len(kayitlar)
        }
    )

@app.post("/admin/sifirla")
async def kayitlari_sifirla(db: Session = Depends(get_db)):
    db.query(MesaiKayit).delete()
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)
