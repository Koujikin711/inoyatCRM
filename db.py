import sqlite3
from datetime import datetime

class Database:
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cur = self.conn.cursor()
        self.create_tables()

    def create_tables(self):
        # Таблица юзеров с полем времени последнего лида для очереди
        self.cur.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, 
            fio TEXT, 
            role TEXT, 
            status TEXT DEFAULT 'pending',
            last_lead_at TIMESTAMP DEFAULT '2000-01-01 00:00:00')""")
        
        self.cur.execute("""CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_phone TEXT,
            manager_id INTEGER,
            status TEXT DEFAULT 'in_progress',
            total_price REAL DEFAULT 0,
            paid_amount REAL DEFAULT 0,
            debt REAL DEFAULT 0,
            reject_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        self.conn.commit()

    def get_next_manager(self):
        """Алгоритм очереди: берем того, кто дольше всех не получал лид"""
        self.cur.execute("""
            SELECT id FROM users 
            WHERE status='active' AND role='manager' 
            ORDER BY last_lead_at ASC LIMIT 1
        """)
        res = self.cur.fetchone()
        if res:
            manager_id = res[0]
            self.cur.execute("UPDATE users SET last_lead_at=? WHERE id=?", (datetime.now(), manager_id))
            self.conn.commit()
            return manager_id
        return None

    def add_user(self, user_id, fio, role='manager'):
        self.cur.execute("INSERT OR IGNORE INTO users (id, fio, role) VALUES (?, ?, ?)", (user_id, fio, role))
        self.conn.commit()

    def set_user_status(self, user_id, status):
        self.cur.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
        self.conn.commit()

    def delete_user(self, user_id):
        self.cur.execute("DELETE FROM users WHERE id=?", (user_id,))
        self.conn.commit()

    def get_stats(self, start_date=None, end_date=None):
        query = "SELECT COUNT(*), SUM(paid_amount), SUM(debt) FROM leads"
        params = []
        if start_date and end_date:
            query += " WHERE created_at BETWEEN ? AND ?"
            params = [start_date, end_date]
        self.cur.execute(query, params)
        return self.cur.fetchone()

    def close_lead(self, lead_id, status, total=0, paid=0, reason=""):
        debt = total - paid
        self.cur.execute("UPDATE leads SET status=?, total_price=?, paid_amount=?, debt=?, reject_reason=? WHERE id=?",
                         (status, total, paid, debt, reason, lead_id))
        self.conn.commit()

    def get_lead_by_phone_manager(self, client_phone, manager_id):
        """Получить лид по номеру и менеджеру (для завершения)."""
        self.cur.execute(
            "SELECT id, client_phone, manager_id, status FROM leads WHERE client_phone=? AND manager_id=? AND status='in_progress' LIMIT 1",
            (client_phone, manager_id)
        )
        return self.cur.fetchone()

    def get_leads_in_progress(self):
        """Список лидов в работе для раздела «Приход»."""
        self.cur.execute(
            "SELECT id, client_phone, paid_amount, debt, total_price FROM leads WHERE status='in_progress' ORDER BY created_at DESC"
        )
        return self.cur.fetchall()

    def get_active_managers(self):
        """Список активных менеджеров для раздела «Уволить»."""
        self.cur.execute(
            "SELECT id, fio FROM users WHERE role='manager' AND status='active' ORDER BY fio"
        )
        return self.cur.fetchall()

    def get_all_leads_for_export(self):
        """Все лиды для выгрузки в архив."""
        self.cur.execute(
            """SELECT id, client_phone, manager_id, status, total_price, paid_amount, debt, reject_reason, created_at 
               FROM leads ORDER BY created_at DESC"""
        )
        return self.cur.fetchall()

    def add_payment_to_lead(self, lead_id, amount):
        """Добавить приход по сделке: увеличить paid_amount и пересчитать debt."""
        self.cur.execute("SELECT paid_amount, total_price FROM leads WHERE id=?", (lead_id,))
        row = self.cur.fetchone()
        if not row:
            return False
        paid_before, total = row[0] or 0, row[1] or 0
        paid_after = paid_before + float(amount)
        debt = max(0, total - paid_after)
        self.cur.execute("UPDATE leads SET paid_amount=?, debt=? WHERE id=?", (paid_after, debt, lead_id))
        self.conn.commit()
        return True