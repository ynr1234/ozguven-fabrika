import os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

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
    durum = Column(String(20))
    mazeret = Column(String(255), nullable=True)
    guncelleme_zamani = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Pazar Mesaisi Bildirim Sistemi")
templates = Jinja2Templates(directory="templates")

# --- FABRİKA PERSONEL LİSTESİ ---
# Format: "SicilNo - Ad Soyad". Bu listeyi kendi güncel bordronuza göre uzatabilirsiniz.
PERSONEL_LISTESI = [
    "1001 - Ahmet Yılmaz",
    "1002 - Caner Şahin",
    "1003 - Elif Öztürk",
    "1004 - Mehmet Demir",
    "1005 - Seda Yılmaz",
    "1006 - Veli Kaya"
]

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
        context={"mesaj": None, "personeller": PERSONEL_LISTESI}
    )

@app.post("/bildir", response_class=HTMLResponse)
async def bildirim_yap(
    request: Request,
    personel_bilgisi: str = Form(...),
    durum: str = Form(...),
    mazeret: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    # Formdan "1001 - Ahmet Yılmaz" olarak gelen veriyi ikiye ayırıyoruz
    try:
        sicil_no, ad_soyad = personel_bilgisi.split(" - ", 1)
    except ValueError:
        sicil_no = "HATA"
        ad_soyad = personel_bilgisi

    mevcut_kayit = db.query(MesaiKayit).filter(MesaiKayit.sicil_no == sicil_no).first()
    
    if mevcut_kayit:
        mevcut_kayit.ad_soyad = ad_soyad
        mevcut_kayit.durum = durum
        mevcut_kayit.mazeret = mazeret
        mevcut_kayit.guncelleme_zamani = datetime.utcnow()
    else:
        yeni_kayit = MesaiKayit(
            sicil_no=sicil_no,
            ad_soyad=ad_soyad,
            durum=durum,
            mazeret=mazeret
        )
        db.add(yeni_kayit)
    
    db.commit()
    
    bilgi = f"Kaydınız başarıyla güncellendi: {durum}"
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"mesaj": bilgi, "durum": durum, "personeller": PERSONEL_LISTESI}
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
