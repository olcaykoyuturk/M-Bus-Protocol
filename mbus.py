import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports
import threading
import time
import sqlite3
from datetime import datetime, timedelta, date
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import calendar

START        = 0x68
STOP         = 0x16
CTRL_REQ_UD2 = 0x5B
NUM_SLAVES   = 8
BAUDRATE     = 9600
TIMEOUT      = 2.0
DB_PATH      = "mbus_data.db"
POLL_INTERVAL = 5

def calc_checksum(data: bytes) -> int:
    return sum(data) & 0xFF

def build_request(addr: int) -> bytes:
    L = 2
    frame = bytearray([START, L, L, START, CTRL_REQ_UD2, addr])
    fcs = calc_checksum(frame[4:4+L])
    frame += bytes([fcs, STOP])
    return bytes(frame)

def parse_long_frame(frame: bytes):
    if len(frame) < 4 + 2 + 1:
        return None
    if frame[0]!=START or frame[3]!=START or frame[1]!=frame[2]:
        return None
    L = frame[1]
    expected_len = 4 + L + 2
    if len(frame) != expected_len:
        return None
    payload = frame[4:4+L]
    ctrl, addr, CI = payload[0], payload[1], payload[2]
    # ID alanı örnek: payload[3:7]  (cihazına göre değişebilir)
    slave_id_bytes = payload[3:7] if len(payload) >= 7 else b'\x00\x00\x00\x00'
    slave_id_hex = slave_id_bytes.hex().upper()
    bcd = payload[5:5+4]
    fcs_recv = frame[4+L]
    if calc_checksum(frame[4:4+L]) != fcs_recv:
        print(f"[FCS HATALI] addr={addr}, beklenen={calc_checksum(frame[4:4+L]):02X}, alinan={fcs_recv:02X}")
        return None
    scaled = 0
    for i, byte in enumerate(bcd):
        hi = (byte >> 4) & 0xF
        lo = byte & 0xF
        scaled += (hi*10 + lo) * (100**i)
    return addr, scaled/100.0, slave_id_hex

def read_frame(ser: serial.Serial):
    buf = bytearray()
    start_time = time.time()
    while time.time() - start_time < TIMEOUT:
        b = ser.read(1)
        if not b:
            continue
        buf += b
        if buf[0] != START:
            buf.pop(0)
            continue
        if len(buf) >= 4 and buf[1]==buf[2] and buf[3]==START:
            L = buf[1]
            total_len = 4 + L + 2
            while len(buf) < total_len and time.time() - start_time < TIMEOUT:
                chunk = ser.read(total_len - len(buf))
                if not chunk:
                    break
                buf += chunk
            return bytes(buf)
    return None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            slave_id INTEGER,
            value REAL
        )
    """)
    conn.commit()
    conn.close()

def insert_reading(slave_id, value):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO readings (timestamp, slave_id, value) VALUES (?, ?, ?)",
        (datetime.now().isoformat(), slave_id, value)
    )
    conn.commit()
    conn.close()

def fetch_trend(days=7):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT date(timestamp), SUM(value)
        FROM readings
        WHERE timestamp >= date('now', ?)
        GROUP BY date(timestamp)
        ORDER BY date(timestamp)
    """, (f'-{days-1} day',))
    rows = cur.fetchall()
    conn.close()
    return rows

def fetch_all_for_compare(period):
    now = datetime.now()
    if period == "Aylık":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT slave_id, SUM(value) FROM readings
        WHERE timestamp >= ?
        GROUP BY slave_id
    """, (start.isoformat(),))
    rows = cur.fetchall()
    conn.close()
    return rows

def fetch_peak_with_threshold(threshold=300):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT slave_id, value, timestamp
        FROM readings
        WHERE value >= ?
        ORDER BY value DESC
        LIMIT 1
    """, (threshold,))
    row = cur.fetchone()
    conn.close()
    return row

def fetch_latest_slave_readings():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT slave_id, value, MAX(timestamp)
        FROM readings
        GROUP BY slave_id
        ORDER BY slave_id
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def list_ports():
    return list(serial.tools.list_ports.comports())

class MBusGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("M-Bus")
        self.running = False
        self.ser = None
        self.selected_port = tk.StringVar()
        self.slave_data = {i: "---" for i in range(1, NUM_SLAVES+1)}
        self.slave_ids = {i: "----" for i in range(1, NUM_SLAVES+1)}
        self.last_read_time = None
        self.build_gui()
        self.update_ports()
        init_db()
        self.show_welcome()

    def build_gui(self):
        self.root.geometry("1270x720")
        self.root.resizable(False, False)
        menu_frame = tk.Frame(self.root, width=240, bg="#ececec")
        menu_frame.pack(side="left", fill="y")
        self.btn_welcome = tk.Button(menu_frame, text="Ana Sayfa", font=("Segoe UI", 12, "bold"), command=self.show_welcome)
        self.btn_welcome.pack(fill="x", pady=(24,10), padx=14)
        self.btn_live = tk.Button(menu_frame, text="Live Slave", font=("Segoe UI", 12, "bold"), command=self.show_live)
        self.btn_live.pack(fill="x", pady=10, padx=14)
        self.btn_report = tk.Button(menu_frame, text="Raporlar", font=("Segoe UI", 12, "bold"), command=self.show_report)
        self.btn_report.pack(fill="x", pady=10, padx=14)
        tk.Label(menu_frame, text="Port Seç:", bg="#ececec", font=("Segoe UI", 11)).pack(pady=(44,6))
        self.port_combo = ttk.Combobox(menu_frame, textvariable=self.selected_port, state="readonly", font=("Segoe UI", 10))
        self.port_combo.pack(fill="x", padx=14)
        self.port_combo.bind("<<ComboboxSelected>>", self.on_port_selected)
        self.btn_exit = tk.Button(menu_frame, text="Çıkış", bg="#f44336", fg="white", font=("Segoe UI", 12, "bold"), command=self.root.quit)
        self.btn_exit.pack(side="bottom", fill="x", pady=26, padx=14)
        self.main_frame = tk.Frame(self.root, bg="white")
        self.main_frame.pack(side="left", fill="both", expand=True)
        self.panel_welcome = self.create_welcome_panel(self.main_frame)
        self.panel_live = self.create_live_panel(self.main_frame)
        self.panel_report = self.create_report_panel(self.main_frame)

    def create_welcome_panel(self, parent):
        frame = tk.Frame(parent, bg="white")
        tk.Label(frame, text="Hoşgeldiniz!", font=("Segoe UI", 25, "bold"), bg="white", fg="#1565c0").pack(pady=(70, 18))
        tk.Label(
            frame,
            text="Bu uygulama ile bağlı olduğunuz M-Bus slave cihazlarının \n"
                 "anlık su tüketimlerini görebilir, geçmişe yönelik detaylı raporlar ve grafikler oluşturabilirsiniz.\n\n"
                 "Sol menüden Live Slave veya Raporlar sekmesine geçebilirsiniz.",
            font=("Segoe UI", 14),
            bg="white"
        ).pack(pady=12)
        tk.Label(
            frame,
            text="Proje Sahibi Ve Geliştirici: Olcay Koyutürk\n2025",
            font=("Segoe UI", 10, "italic"),
            bg="white", fg="#888"
        ).pack(pady=(70, 4))
        return frame

    def create_live_panel(self, parent):
        frame = tk.Frame(parent, bg="white")
        self.last_time_lbl = tk.Label(frame, text="", bg="white", font=("Segoe UI", 12, "italic"), fg="#5a5a5a")
        self.last_time_lbl.pack(anchor="w", padx=22, pady=(12, 0))
        tk.Label(frame, text="Live Slave Verileri", font=("Segoe UI", 16, "bold"), bg="white", fg="#1a237e").pack(anchor="w", pady=8, padx=18)
        columns = ("Slave", "ID", "Değer (m³)")
        self.slave_table = ttk.Treeview(frame, columns=columns, show="headings", height=NUM_SLAVES)
        self.slave_table.heading("Slave", text="Slave")
        self.slave_table.heading("ID", text="Slave ID")
        self.slave_table.heading("Değer (m³)", text="Değer (m³)")
        self.slave_table.column("Slave", width=90, anchor="center")
        self.slave_table.column("ID", width=120, anchor="center")
        self.slave_table.column("Değer (m³)", width=120, anchor="center")
        style = ttk.Style()
        style.configure("Treeview.Heading", font=("Segoe UI", 12, "bold"), foreground="#222")
        style.configure("Treeview", font=("Segoe UI", 12), rowheight=32)
        self.slave_table.pack(padx=22, pady=10)
        return frame

    def create_report_panel(self, parent):
        frame = tk.Frame(parent, bg="white")
        self.summary_frame = tk.Frame(frame, bg="white")
        self.summary_frame.pack(anchor="w", pady=(12,0), padx=14)
        top = tk.Frame(frame, bg="white")
        top.pack(anchor="w", pady=8, padx=12)
        tk.Label(top, text="Raporlama", font=("Segoe UI", 16, "bold"), bg="white", fg="#1a237e").pack(side="left")
        self.period_combo = ttk.Combobox(top, values=[
            "Günlük", "Haftalık", "Aylık", "Yıllık",
            "Ortalama Tüketim", "Daire Karşılaştırma", "Trend Grafiği", "Pik Kullanım"
        ], state="readonly", width=24, font=("Segoe UI", 11))
        self.period_combo.current(0)
        self.period_combo.pack(side="left", padx=12)
        self.period_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_report())
        self.pdf_btn = tk.Button(top, text="PDF Olarak Kaydet", font=("Segoe UI", 10), command=self.export_pdf)
        self.pdf_btn.pack(side="left", padx=(12, 8))
        # Pik kullanım için threshold alanı
        self.threshold_var = tk.IntVar(value=300)
        self.threshold_label = tk.Label(top, text="Eşik (m³):", font=("Segoe UI", 11), bg="white")
        self.threshold_entry = tk.Entry(top, width=7, textvariable=self.threshold_var, font=("Segoe UI", 11))
        self.threshold_btn = tk.Button(top, text="Güncelle", font=("Segoe UI", 10), command=self.refresh_report)
        self.report_table = ttk.Treeview(frame)
        self.report_table.pack(padx=22, pady=(10,10))
        self.fig = Figure(figsize=(8, 3.5))
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.graph_widget = self.canvas.get_tk_widget()
        self.report_table.bind("<Double-1>", self.show_slave_history)
        return frame

    def export_pdf(self):
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Spacer, Image, Paragraph
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        import tempfile
        import os
        from datetime import datetime
        import time
        from tkinter import filedialog

        # 1. Kullanıcıya kayıt yeri sor
        defaultname = f"rapor_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.pdf"
        fname = filedialog.asksaveasfilename(
            title="PDF Olarak Kaydet",
            defaultextension=".pdf",
            initialfile=defaultname,
            filetypes=[("PDF Dosyası", "*.pdf")]
        )
        if not fname:
            return  # İptal edildiyse

        doc = SimpleDocTemplate(fname, pagesize=A4)

        # 2. Tablo verisi
        columns = self.report_table["columns"]
        data = [columns]
        for item in self.report_table.get_children():
            data.append(list(self.report_table.item(item)["values"]))

        # 3. Tabloyu oluştur
        tbl = Table(data, hAlign="LEFT")
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1976d2")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("BACKGROUND", (0, 1), (-1, -1), colors.whitesmoke),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.gray),
        ]))

        # 4. Grafik varsa PNG olarak kaydet (geçici dosya)
        story = []
        story.append(Paragraph(f"<b>Rapor: {self.period_combo.get()}</b>", getSampleStyleSheet()["Title"]))
        story.append(Spacer(1, 16))
        story.append(tbl)
        story.append(Spacer(1, 16))
        has_graph = False
        tmpimg_name = None
        if hasattr(self, 'ax') and len(self.ax.patches) > 0:
            tmpimg = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            self.fig.savefig(tmpimg.name, bbox_inches='tight')
            tmpimg.close()
            tmpimg_name = tmpimg.name
            story.append(Image(tmpimg_name, width=430, height=200))
            has_graph = True

        # 5. PDF’i oluştur
        doc.build(story)

        # 6. Geçici dosya varsa sil (Windows fix)
        if has_graph and tmpimg_name:
            for _ in range(10):
                try:
                    os.remove(tmpimg_name)
                    break
                except PermissionError:
                    time.sleep(0.2)

        # 7. Başarı mesajı
        messagebox.showinfo("PDF Kaydedildi", f"Rapor PDF olarak kaydedildi:\n{fname}")

    def show_slave_history(self, event):
        import tkinter as tk
        from tkinter import ttk
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        import sqlite3
        from datetime import date, timedelta

        item = self.report_table.selection()
        if not item:
            return
        values = self.report_table.item(item[0])["values"]
        slave_str = str(values[0])
        if "Slave" in slave_str:
            sid = int(slave_str.split()[1])
        else:
            return

        win = tk.Toplevel(self.root)
        win.title(f"Slave {sid} – Günlük Analiz")
        win.geometry("1020x800")
        win.configure(bg="white")

        # Kaydırılabilir ana alan
        canvas = tk.Canvas(win, bg="white", highlightthickness=0)
        scrollbar = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg="white")
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ---- MODERN BAŞLIK ----
        header = tk.Label(scroll_frame, text=f"Slave {sid} Su Tüketim Geçmişi", font=("Segoe UI", 18, "bold"),
                          bg="white", fg="#203880", pady=10)
        header.pack(pady=(8, 4))

        # ---- ŞIK İSTATİSTİKLER SATIRI ----
        stats_frame = tk.Frame(scroll_frame, bg="white")
        stats_frame.pack(fill="x", pady=(0, 16))
        stats_labels = []
        for i in range(3):
            lbl = tk.Label(stats_frame, text="", font=("Segoe UI", 12, "bold"), bg="white", fg="#173271", anchor="w",
                           justify="left")
            lbl.grid(row=0, column=i, padx=22, sticky="w")
            stats_labels.append(lbl)
        for i in range(3, 6):
            lbl = tk.Label(stats_frame, text="", font=("Segoe UI", 12, "bold"), bg="white", fg="#173271", anchor="w",
                           justify="left")
            lbl.grid(row=1, column=i - 3, padx=22, sticky="w")
            stats_labels.append(lbl)

        # ---- GRAFİK BÖLÜMÜ ----
        graph_frame = tk.Frame(scroll_frame, bg="white")
        graph_frame.pack(fill="x", pady=(0, 12))
        fig = Figure(figsize=(9.7, 3.2))
        ax = fig.add_subplot(111)
        canvas_mpl = FigureCanvasTkAgg(fig, master=graph_frame)
        canvas_mpl.get_tk_widget().pack(fill="x", expand=True)

        # ---- TABLO (Günlük tüketim) ----
        table_frame = tk.Frame(scroll_frame, bg="white")
        table_frame.pack(fill="both", expand=True, pady=(0, 8))
        style = ttk.Style()
        style.configure("Treeview", font=("Segoe UI", 11), rowheight=28)
        style.configure("Treeview.Heading", font=("Segoe UI", 11, "bold"))
        table = ttk.Treeview(table_frame, columns=("Gün", "Tüketim (m³)"), show="headings", height=15)
        table.heading("Gün", text="Gün")
        table.heading("Tüketim (m³)", text="Tüketim (m³)")
        table.column("Gün", width=85, anchor="center")
        table.column("Tüketim (m³)", width=160, anchor="center")
        tscroll = tk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=tscroll.set)
        table.pack(side="left", fill="both", expand=True, padx=20)
        tscroll.pack(side="right", fill="y")

        # ---- SLIDER TAM ORTADA VE ALTA ----
        slider_frame = tk.Frame(scroll_frame, bg="white")
        slider_frame.pack(fill="x", pady=(10, 0))
        slider_frame.grid_columnconfigure(0, weight=1)
        slider_frame.grid_columnconfigure(1, weight=1)
        slider_frame.grid_columnconfigure(2, weight=1)
        tk.Label(slider_frame, text="Gün Aralığı:", font=("Segoe UI", 11, "bold"), bg="white").grid(row=0, column=0,
                                                                                                    sticky="e")
        slider = tk.Scale(slider_frame, from_=7, to=60, orient="horizontal", length=340, showvalue=True,
                          font=("Segoe UI", 10))
        slider.set(30)
        slider.grid(row=0, column=1)
        tk.Label(slider_frame, text="(7-60)", font=("Segoe UI", 10), bg="white", fg="#666").grid(row=0, column=2,
                                                                                                 sticky="w")

        # ---- KAPAT TUŞU TAM EN ALTA ----
        tk.Button(scroll_frame, text="Kapat", command=win.destroy, font=("Segoe UI", 11, "bold"), bg="#e53935",
                  fg="white", padx=16, pady=3, relief="ridge").pack(pady=18)

        # ---- GÜNCELLEME FONKSİYONU ----
        def update_panel(days_count):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            today = date.today()
            tarih_liste = [(today - timedelta(days=i)) for i in range(days_count - 1, -1, -1)]
            tarih_str_liste = [d.strftime("%Y-%m-%d") for d in tarih_liste]
            days_ = []
            vals_ = []
            for d in tarih_str_liste:
                cur.execute("""
                            SELECT SUM(value)
                            FROM readings
                            WHERE slave_id = ? AND date (timestamp)=?
                            """, (sid, d))
                res = cur.fetchone()
                val = res[0] if res[0] is not None else 0
                days_.append(d[-5:])  # ay-gün
                vals_.append(val)
            toplam = sum(vals_)
            ort = toplam / len(vals_) if vals_ else 0
            vmax = max(vals_) if vals_ else 0
            vmin = min([v for v in vals_ if v > 0] or [0])
            try:
                pik_idx = vals_.index(vmax)
                pik_gun = days_[pik_idx]
            except:
                pik_gun = "-"
            cur.execute("""
                        SELECT MAX(timestamp)
                        FROM readings
                        WHERE slave_id = ?
                        """, (sid,))
            last_read = cur.fetchone()[0]
            conn.close()

            # Tabloya doldur
            table.delete(*table.get_children())
            for g, v in zip(days_, vals_):
                table.insert("", "end", values=(g, f"{v:.2f}"))

            # İstatistikler
            stats = [
                f"Slave: {sid}",
                f"Top: {toplam:.2f} m³",
                f"Ort: {ort:.2f} m³/gün",
                f"Yüksek: {pik_gun} ({vmax:.2f} m³)",
                f"Düşük: {vmin:.2f} m³",
                f"Son Ölçüm: {last_read[:16] if last_read else '-'}"
            ]
            for i, stat in enumerate(stats):
                stats_labels[i].config(text=stat)

            # Grafik güncelle
            ax.clear()
            ax.bar(range(len(days_)), vals_, color="#2196f3", label="Bar")
            ax.plot(range(len(days_)), vals_, color="#e53935", marker="o", linewidth=2, label="Trend")
            ax.set_title(f"Slave {sid} – Son {days_count} Günlük Su Tüketimi", fontsize=13)
            ax.set_xlabel("Gün")
            ax.set_ylabel("Tüketim (m³)")
            ax.legend(fontsize=11)
            ax.set_xticks(range(len(days_)))
            ax.set_xticklabels(days_, rotation=45, ha='right', fontsize=9)
            fig.tight_layout()
            canvas_mpl.draw()

        # İlk açılışta ve slider değişince
        update_panel(slider.get())
        slider.config(command=lambda val: update_panel(int(val)))

        # Kaydırma tekerleğiyle de çalışsın:
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        win.transient(self.root)
        win.grab_set()
        win.focus_set()

    def show_welcome(self):
        self.stop_polling()
        self.panel_live.pack_forget()
        self.panel_report.pack_forget()
        self.panel_welcome.pack(fill="both", expand=True)

    def show_live(self):
        self.panel_welcome.pack_forget()
        self.panel_report.pack_forget()
        self.panel_live.pack(fill="both", expand=True)
        self.start_polling()

    def show_report(self):
        self.panel_welcome.pack_forget()
        self.panel_live.pack_forget()
        self.panel_report.pack(fill="both", expand=True)
        self.refresh_report()
        self.stop_polling()

    def update_ports(self):
        ports = list_ports()
        port_list = [p.device for p in ports]
        self.port_combo["values"] = port_list
        if port_list:
            self.selected_port.set(port_list[0])
        else:
            self.selected_port.set('')

    def on_port_selected(self, event):
        self.connect_port()

    def connect_port(self):
        if self.ser:
            try:
                self.ser.close()
            except:
                pass
        port = self.selected_port.get()
        if not port:
            return
        try:
            self.ser = serial.Serial(port, BAUDRATE, timeout=0.5)
            time.sleep(2)
        except Exception as e:
            messagebox.showerror("Hata", f"Seri port açılamadı: {e}")
            self.ser = None

    def start_polling(self):
        if self.running:
            return
        if not self.ser:
            self.connect_port()
        self.running = True
        self.poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
        self.poll_thread.start()

    def stop_polling(self):
        self.running = False

    def poll_loop(self):
        while self.running and self.ser:
            updated = False
            for addr in range(1, NUM_SLAVES+1):
                try:
                    self.ser.reset_input_buffer()
                    req = build_request(addr)
                    self.ser.write(req)
                    time.sleep(0.5)
                    frame = read_frame(self.ser)
                    if frame:
                        print("GELEN FRAME:", frame.hex())
                        res = parse_long_frame(frame)
                        if res:
                            a, v, slaveid = res
                            self.slave_data[a] = f"{v:.2f}"
                            self.slave_ids[a] = slaveid
                            insert_reading(a, v)
                            updated = True
                        else:
                            self.slave_data[addr] = "ERR"
                            self.slave_ids[addr] = "----"
                    else:
                        self.slave_data[addr] = "---"
                        self.slave_ids[addr] = "----"
                except Exception as ex:
                    self.slave_data[addr] = "ERR"
                    self.slave_ids[addr] = "----"
                    print(f"Slave {addr} hata: {ex}")
            if updated:
                self.last_read_time = datetime.now()
            self.update_live_table()
            for _ in range(POLL_INTERVAL * 2):
                if not self.running:
                    break
                time.sleep(0.5)

    def update_live_table(self):
        for i in self.slave_table.get_children():
            self.slave_table.delete(i)
        now = self.last_read_time.strftime('%d.%m.%Y %H:%M:%S') if self.last_read_time else "-"
        self.last_time_lbl.config(text=f"Son Okuma: {now}")
        for sid in range(1, NUM_SLAVES+1):
            val = self.slave_data[sid]
            slaveid = self.slave_ids[sid]
            is_ok = val != "ERR" and val != "---"
            icon = "🟢" if is_ok else "🔴"
            tag = "ok" if val != "ERR" else "err"
            self.slave_table.insert("", "end", values=(f"{icon} Slave {sid}", slaveid, val), tags=(tag,))
        self.slave_table.tag_configure('ok', background="#e7ffe9")
        self.slave_table.tag_configure('err', background="#ffeaea")

    def reset_table(self):
        for col in self.report_table["columns"]:
            self.report_table.heading(col, text="")
            self.report_table.column(col, width=0)
        self.report_table["columns"] = []
        self.report_table["show"] = "headings"
        for i in self.report_table.get_children():
            self.report_table.delete(i)
        self.report_table.pack(padx=22, pady=(10,10))

    def refresh_report(self):
        self.graph_widget.pack_forget()
        self.ax.clear()
        for widget in self.summary_frame.winfo_children():
            widget.destroy()
        period = self.period_combo.get() or "Günlük"

        # Pik Kullanım sekmesinde threshold kutusu görünür, diğerlerinde gizli
        if period == "Pik Kullanım":
            self.threshold_label.pack(side="left", padx=(30, 2))
            self.threshold_entry.pack(side="left")
            self.threshold_btn.pack(side="left", padx=(2,8))
        else:
            self.threshold_label.pack_forget()
            self.threshold_entry.pack_forget()
            self.threshold_btn.pack_forget()

        # --- Trend Grafiği sekmesi: sadece grafik, tablo gizli ---
        if period == "Trend Grafiği":
            self.report_table.pack_forget()
            rows = fetch_trend(days=7)
            if rows:
                days = [row[0][-5:] for row in rows]
                vals = [row[1] for row in rows]
                self.ax.plot(days, vals, marker="o", linewidth=2, color="#1565c0")
                self.ax.set_title("Son 7 Gün Tüketim Trend Grafiği", fontsize=13, weight="bold")
                self.ax.set_xlabel("Tarih")
                self.ax.set_ylabel("Toplam m³")
                self.fig.tight_layout()
                self.canvas.draw()
                self.graph_widget.pack(padx=22, pady=10)
            return
        else:
            self.reset_table()

        slave_cols = [f"Slave {sid}" for sid in range(1, NUM_SLAVES+1)]
        columns = []
        x_labels, total_vals = [], []
        slave_vals = {sid: [] for sid in range(1, NUM_SLAVES+1)}

        if period == "Günlük":
            saatler = [str(i).zfill(2) for i in range(24)]
            columns = ["Saat"] + slave_cols + ["Toplam"]
            self.report_table["columns"] = columns
            for col in columns:
                self.report_table.heading(col, text=col)
                self.report_table.column(col, width=90, anchor="center")
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            slave_data = {s: {sl: 0 for sl in range(1, NUM_SLAVES+1)} for s in saatler}
            cur.execute("""
                SELECT strftime('%H', timestamp) as saat, slave_id, SUM(value)
                FROM readings
                WHERE timestamp >= datetime('now', 'start of day')
                GROUP BY saat, slave_id
            """)
            for saat, sid, toplam in cur.fetchall():
                slave_data[saat][sid] = toplam
            conn.close()
            for saat in saatler:
                row = [f"{saat}:00"]
                toplam = 0
                for sid in range(1, NUM_SLAVES+1):
                    val = slave_data[saat][sid]
                    row.append(f"{val:.2f}")
                    toplam += val
                    slave_vals[sid].append(val)
                row.append(f"{toplam:.2f}")
                self.report_table.insert("", "end", values=row)
                total_vals.append(toplam)
                x_labels.append(saat)
        elif period == "Haftalık":
            gun_ad = ['Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt', 'Paz']
            today = date.today()
            tarih_liste = [(today - timedelta(days=(today.weekday()-i)%7)).strftime("%Y-%m-%d") for i in range(7)]
            columns = ["Gün"] + slave_cols + ["Toplam"]
            self.report_table["columns"] = columns
            for col in columns:
                self.report_table.heading(col, text=col)
                self.report_table.column(col, width=90, anchor="center")
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            slave_data = {t: {sl: 0 for sl in range(1, NUM_SLAVES+1)} for t in tarih_liste}
            cur.execute("""
                SELECT date(timestamp), slave_id, SUM(value)
                FROM readings
                WHERE timestamp >= date('now', '-6 days')
                GROUP BY date(timestamp), slave_id
            """)
            for t, sid, toplam in cur.fetchall():
                slave_data[t][sid] = toplam
            conn.close()
            for d in tarih_liste:
                gunidx = datetime.strptime(d, "%Y-%m-%d").weekday()
                row = [f"{gun_ad[gunidx]} ({d[-5:]})"]
                toplam = 0
                for sid in range(1, NUM_SLAVES+1):
                    val = slave_data[d][sid]
                    row.append(f"{val:.2f}")
                    toplam += val
                    slave_vals[sid].append(val)
                row.append(f"{toplam:.2f}")
                self.report_table.insert("", "end", values=row)
                total_vals.append(toplam)
                x_labels.append(gun_ad[gunidx])
        elif period == "Aylık":
            self.reset_table()
            today = date.today()
            first_day = today.replace(day=1)
            gunler = []
            tarih_str_liste = []
            d = first_day
            while d <= today:
                gunler.append(d.strftime("%d"))
                tarih_str_liste.append(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            columns = ["Gün"] + slave_cols + ["Toplam"]
            self.report_table["columns"] = columns
            for col in columns:
                self.report_table.heading(col, text=col)
                self.report_table.column(col, width=90, anchor="center")
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            slave_data = {d: {sl: 0 for sl in range(1, NUM_SLAVES+1)} for d in tarih_str_liste}
            cur.execute("""
                SELECT date(timestamp), slave_id, SUM(value)
                FROM readings
                WHERE timestamp >= ?
                GROUP BY date(timestamp), slave_id
            """, (tarih_str_liste[0],))
            for t, sid, toplam in cur.fetchall():
                if t in slave_data:
                    slave_data[t][sid] = toplam
            conn.close()
            for g, d in zip(gunler, tarih_str_liste):
                row = [f"{g} ({d[-5:]})"]
                toplam = 0
                for sid in range(1, NUM_SLAVES+1):
                    val = slave_data[d][sid]
                    row.append(f"{val:.2f}")
                    toplam += val
                    slave_vals[sid].append(val)
                row.append(f"{toplam:.2f}")
                self.report_table.insert("", "end", values=row)
                x_labels.append(g)
                total_vals.append(toplam)
            self.ax.clear()
            if x_labels and total_vals:
                self.ax.bar(x_labels, total_vals, color="#1976d2")
                self.ax.set_title("Aylık Toplam Su Tüketimi")
                self.ax.set_xlabel("Gün")
                self.ax.set_ylabel("Tüketim (m³)")
                self.fig.tight_layout()
                self.canvas.draw()
                self.graph_widget.pack(padx=22, pady=10)
        elif period == "Yıllık":
            thisyear = date.today().year
            aylar = [calendar.month_abbr[m] for m in range(1,13)]
            yilsira = [f"{thisyear}-{str(m).zfill(2)}" for m in range(1,13)]
            columns = ["Ay"] + slave_cols + ["Toplam"]
            self.report_table["columns"] = columns
            for col in columns:
                self.report_table.heading(col, text=col)
                self.report_table.column(col, width=90, anchor="center")
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            slave_data = {y: {sl: 0 for sl in range(1, NUM_SLAVES+1)} for y in yilsira}
            cur.execute("""
                SELECT strftime('%Y-%m', timestamp), slave_id, SUM(value)
                FROM readings
                WHERE timestamp >= date('now', 'start of year')
                GROUP BY strftime('%Y-%m', timestamp), slave_id
            """)
            for yyyymm, sid, toplam in cur.fetchall():
                if yyyymm in slave_data:
                    slave_data[yyyymm][sid] = toplam
            conn.close()
            for ay, y in zip(aylar, yilsira):
                row = [ay]
                toplam = 0
                for sid in range(1, NUM_SLAVES+1):
                    val = slave_data[y][sid]
                    row.append(f"{val:.2f}")
                    toplam += val
                    slave_vals[sid].append(val)
                row.append(f"{toplam:.2f}")
                self.report_table.insert("", "end", values=row)
                total_vals.append(toplam)
                x_labels.append(ay)
        elif period == "Ortalama Tüketim":
            self.report_table["columns"] = ["Slave", "Anlık Tüketim (m³)"]
            self.report_table.heading("Slave", text="Slave")
            self.report_table.heading("Anlık Tüketim (m³)", text="Anlık Tüketim (m³)")
            self.report_table.column("Slave", width=160, anchor="center")
            self.report_table.column("Anlık Tüketim (m³)", width=160, anchor="center")
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            for sid in range(1, NUM_SLAVES + 1):
                cur.execute("""
                    SELECT value FROM readings
                    WHERE slave_id=?
                    ORDER BY timestamp DESC
                    LIMIT 2
                """, (sid,))
                vals = cur.fetchall()
                if len(vals) == 2:
                    anlik = vals[0][0] - vals[1][0]
                    if anlik < 0:
                        anlik = 0
                else:
                    anlik = 0
                self.report_table.insert("", "end", values=(f"Slave {sid}", f"{anlik:.2f}"))
            conn.close()
        elif period == "Daire Karşılaştırma":
            self.report_table["columns"] = ["Slave", "Aylık Toplam (m³)"]
            self.report_table.heading("Slave", text="Slave")
            self.report_table.heading("Aylık Toplam (m³)", text="Aylık Toplam (m³)")
            self.report_table.column("Slave", width=160, anchor="center")
            self.report_table.column("Aylık Toplam (m³)", width=160, anchor="center")
            data = fetch_all_for_compare("Aylık")
            if data:
                for sid, total in sorted(data, key=lambda x: -x[1]):
                    self.report_table.insert("", "end", values=(f"Slave {sid}", f"{total:.2f}"))
        elif period == "Pik Kullanım":
            self.report_table["columns"] = ["Slave", "En Yüksek Anlık (m³)"]
            self.report_table.heading("Slave", text="Slave")
            self.report_table.heading("En Yüksek Anlık (m³)", text="En Yüksek Anlık (m³)")
            self.report_table.column("Slave", width=160, anchor="center")
            self.report_table.column("En Yüksek Anlık (m³)", width=200, anchor="center")
            try:
                threshold = int(self.threshold_var.get())
            except Exception:
                threshold = 300  # Default
            row = fetch_peak_with_threshold(threshold)
            if row:
                self.report_table.insert(
                    "", "end",
                    values=(f"Slave {row[0]}", f"{row[1]:.2f} m³ ({row[2][:16]})")
                )
                self.report_table.tag_configure('pik', background="#ffe082")
                last = self.report_table.get_children()[-1]
                self.report_table.item(last, tags=('pik',))
            else:
                self.report_table.insert("", "end", values=("", f"Eşik üstü değer yok (>{threshold} m³)"))
        # Grafik: toplam tüketim (isteğe göre slave bazında da gösterebiliriz)
        if x_labels and total_vals and period not in ["Aylık", "Trend Grafiği"]:
            self.ax.bar(x_labels, total_vals, color="#1976d2")
            self.ax.set_title(f"{period} Toplam Su Tüketimi")
            self.ax.set_xlabel("Zaman")
            self.ax.set_ylabel("Tüketim (m³)")
            self.fig.tight_layout()
            self.canvas.draw()
            self.graph_widget.pack(padx=22, pady=10)

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = MBusGUI(root)
    root.mainloop()
