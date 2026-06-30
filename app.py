"""
Price Monitor — desktop browser (PySide6).

Search products by name, view current price / discount / stock in a table,
and see the price-history chart for the selected product.

Run:  python app.py            (uses prices.db in the current folder)
      python app.py other.db   (point at a different database)

Requires: pip install PySide6
"""

import sys
import urllib.request
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTableWidget, QTableWidgetItem, QLabel, QCheckBox,
    QSplitter, QHeaderView, QAbstractItemView, QPushButton, QMessageBox,
)
from PySide6.QtCore import Qt, QTimer, QPointF, QThread, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtCharts import (
    QChart, QChartView, QLineSeries, QScatterSeries,
    QValueAxis, QDateTimeAxis,
)

from storage import Store


def parse_ts(s):
    """Parse stored ISO8601 timestamp to a datetime."""
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


class ImageLoader(QThread):
    """Fetch a product image URL off the UI thread; emit the raw bytes."""
    loaded = Signal(str, bytes)   # url, data

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": "PriceMonitor/0.1"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
            self.loaded.emit(self.url, data)
        except Exception:
            self.loaded.emit(self.url, b"")


class SyncWorker(QThread):
    """Download the published snapshot and merge it into the DB, off the UI thread."""
    done = Signal(dict)      # result counts
    failed = Signal(str)     # error message

    def __init__(self, db_path, repo="labrouss/pricemonitor"):
        super().__init__()
        self.db_path = db_path
        self.repo = repo

    def run(self):
        try:
            import sync_snapshot
            from storage import Store
            store = Store(self.db_path)      # own connection for this thread
            try:
                res = sync_snapshot.run(store, repo=self.repo)
            finally:
                store.close()
            self.done.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


class PriceMonitorApp(QMainWindow):
    def __init__(self, db_path="prices.db"):
        super().__init__()
        self.db_path = db_path
        self.store = Store(db_path)
        self.results = []  # current search results (dicts)

        self.setWindowTitle("Price Monitor")
        self.resize(1100, 700)

        # ---- Search bar ----
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search products by name…")
        self.search_box.setClearButtonEnabled(True)

        self.cb_offer = QCheckBox("On offer")
        self.cb_stock = QCheckBox("In stock")

        self.btn_sync = QPushButton("⤓ Sync data")
        self.btn_sync.setToolTip(
            "Download the latest published prices and merge into this database")
        self.btn_sync.clicked.connect(self.on_sync)

        top = QHBoxLayout()
        top.addWidget(self.search_box, 1)
        top.addWidget(self.cb_offer)
        top.addWidget(self.cb_stock)
        top.addWidget(self.btn_sync)

        # ---- Results table ----
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Product", "Price", "Was", "Unit", "Stock", "Retailer"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 6):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)

        # ---- Chart ----
        self.chart = QChart()
        self.chart.setTitle("Select a product to see its price history")
        self.chart.legend().setVisible(True)
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.Antialiasing)

        # Product image panel (to the right of the chart)
        self.image_label = QLabel("No image")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumWidth(200)
        self.image_label.setMaximumWidth(260)
        self.image_label.setStyleSheet(
            "QLabel { border: 1px solid #ccc; background: #fafafa; color: #999; }")
        self._loaders = []
        self._current_image_url = None

        self.status = QLabel("")

        # ---- Layout: table on top; chart + image below (resizable) ----
        chart_row = QWidget()
        cr = QHBoxLayout(chart_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self.chart_view, 1)
        cr.addWidget(self.image_label)

        splitter = QSplitter(Qt.Vertical)
        table_container = QWidget()
        tc = QVBoxLayout(table_container)
        tc.setContentsMargins(0, 0, 0, 0)
        tc.addWidget(self.table)
        splitter.addWidget(table_container)
        splitter.addWidget(chart_row)
        splitter.setSizes([380, 320])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)
        self.setCentralWidget(central)

        # ---- Signals (debounced search) ----
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.run_search)
        self.search_box.textChanged.connect(lambda _: self._debounce.start())
        self.cb_offer.stateChanged.connect(lambda _: self.run_search())
        self.cb_stock.stateChanged.connect(lambda _: self.run_search())
        self.table.itemSelectionChanged.connect(self.on_select)

        self.run_search()  # initial population

    # ---------------------------------------------------------------
    def on_sync(self):
        resp = QMessageBox.question(
            self, "Sync data",
            "Download the latest published price snapshot and merge it into this "
            "database?\n\nNew prices are added as fresh observations.",
            QMessageBox.Yes | QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        self.btn_sync.setEnabled(False)
        self.btn_sync.setText("⤓ Syncing…")
        self.status.setText("Syncing latest prices…")
        self._sync = SyncWorker(self.db_path)
        self._sync.done.connect(self.on_sync_done)
        self._sync.failed.connect(self.on_sync_failed)
        self._sync.start()

    def on_sync_done(self, res):
        self.btn_sync.setEnabled(True)
        self.btn_sync.setText("⤓ Sync data")
        msg = (f"Synced — added {res.get('ingested', 0)} prices"
               + (f", skipped {res['skipped']}" if res.get('skipped') else "")
               + (f", {res['errors']} errors" if res.get('errors') else "") + ".")
        self.status.setText(msg)
        self.run_search()      # refresh the view with new data

    def on_sync_failed(self, err):
        self.btn_sync.setEnabled(True)
        self.btn_sync.setText("⤓ Sync data")
        self.status.setText("Sync failed.")
        QMessageBox.warning(self, "Sync failed",
                            f"Could not sync the snapshot:\n{err}\n\n"
                            "Check that the repo has a published release.")

    def run_search(self):
        q = self.search_box.text().strip()
        self.results = self.store.search_products(
            query=q,
            on_offer=self.cb_offer.isChecked(),
            in_stock_only=self.cb_stock.isChecked(),
            limit=1000,
        )
        self.populate_table()
        self.status.setText(f"{len(self.results)} products"
                            + (f" matching “{q}”" if q else ""))

    def populate_table(self):
        # Block selection signals while we rebuild the table — otherwise
        # setRowCount/setItem fire itemSelectionChanged repeatedly, each
        # triggering a chart redraw + image fetch while the user is typing.
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.results))
        for row, r in enumerate(self.results):
            price = r.get("price")
            listp = r.get("list_price")
            unitp = r.get("unit_price")
            unit = r.get("unit") or ""
            in_stock = r.get("in_stock")

            name_item = QTableWidgetItem(r.get("name") or "?")
            price_item = QTableWidgetItem(f"{price:.2f} €" if price is not None else "—")

            was = ""
            if listp and price is not None and listp > price:
                pct = 100 * (listp - price) / listp
                was = f"{listp:.2f} € (-{pct:.0f}%)"
            was_item = QTableWidgetItem(was)
            if was:
                was_item.setForeground(Qt.red)

            unit_item = QTableWidgetItem(
                f"{unitp:.2f} €/{unit}" if unitp else "")
            stock_txt = {1: "Yes", 0: "No"}.get(in_stock, "?")
            stock_item = QTableWidgetItem(stock_txt)
            if in_stock == 0:
                stock_item.setForeground(Qt.gray)
            retailer_item = QTableWidgetItem(r.get("retailer") or "")

            for col, item in enumerate(
                [name_item, price_item, was_item, unit_item,
                 stock_item, retailer_item]):
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)

    # ---------------------------------------------------------------
    def on_select(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        if idx >= len(self.results):
            return
        product = self.results[idx]
        self.plot_history(product)
        self.load_image(product)

    def load_image(self, product):
        url = product.get("image_url")
        self._current_image_url = url
        if not url:
            self.image_label.setText("No image")
            self.image_label.setPixmap(QPixmap())  # clear
            return
        self.image_label.setText("Loading…")
        self.image_label.setPixmap(QPixmap())
        # Do NOT terminate a running QThread (that can deadlock the UI). Just
        # start a new loader; stale results are ignored via the URL check in
        # on_image_loaded. Keep a reference so it isn't garbage-collected.
        loader = ImageLoader(url)
        loader.loaded.connect(self.on_image_loaded)
        self._loaders = getattr(self, "_loaders", [])
        self._loaders = [l for l in self._loaders if l.isRunning()]  # prune finished
        self._loaders.append(loader)
        loader.start()

    def on_image_loaded(self, url, data):
        if url != self._current_image_url:
            return  # a newer selection superseded this
        if not data:
            self.image_label.setText("No image")
            return
        pix = QPixmap()
        if pix.loadFromData(data):
            self.image_label.setPixmap(
                pix.scaled(self.image_label.maximumWidth(), 240,
                           Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.image_label.setText("")
        else:
            self.image_label.setText("No image")

    def closeEvent(self, event):
        # Wait for any in-flight image loaders to finish (don't terminate).
        for loader in getattr(self, "_loaders", []):
            if loader.isRunning():
                loader.wait(2000)
        super().closeEvent(event)

    def plot_history(self, product):
        history = self.store.price_history(product["offer_id"])
        self.chart.removeAllSeries()
        for ax in list(self.chart.axes()):
            self.chart.removeAxis(ax)

        name = product.get("name") or "?"
        if not history:
            self.chart.setTitle(f"{name} — no history yet")
            return

        price_series = QLineSeries()
        price_series.setName("Price")
        point_series = QScatterSeries()
        point_series.setName("Observations")
        point_series.setMarkerSize(8)

        list_series = QLineSeries()
        list_series.setName("Was (list price)")

        has_list = False
        prices = []
        xs = []
        for h in history:
            ts = parse_ts(h["observed_at"])
            x = ts.timestamp() * 1000  # QDateTimeAxis uses msecs
            if h["price"] is not None:
                price_series.append(x, h["price"])
                point_series.append(x, h["price"])
                prices.append(h["price"])
                xs.append(x)
            if h.get("list_price"):
                list_series.append(x, h["list_price"])
                has_list = True

        self.chart.addSeries(price_series)
        self.chart.addSeries(point_series)
        if has_list:
            self.chart.addSeries(list_series)

        # Axes
        ax_x = QDateTimeAxis()
        ax_x.setFormat("dd MMM")
        ax_x.setTitleText("Date")
        ax_y = QValueAxis()
        ax_y.setTitleText("€")
        if prices:
            lo, hi = min(prices), max(prices)
            if has_list:
                hi = max(hi, max(h["list_price"] for h in history if h.get("list_price")))
            pad = max(0.05, (hi - lo) * 0.15)
            ax_y.setRange(max(0, lo - pad), hi + pad)

        self.chart.addAxis(ax_x, Qt.AlignBottom)
        self.chart.addAxis(ax_y, Qt.AlignLeft)
        for s in self.chart.series():
            s.attachAxis(ax_x)
            s.attachAxis(ax_y)

        n = len(prices)
        cur = prices[-1] if prices else None
        title = f"{name}"
        if cur is not None:
            title += f" — current {cur:.2f} €  ({n} observation{'s' if n != 1 else ''})"
        self.chart.setTitle(title)


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "prices.db"
    app = QApplication(sys.argv)
    win = PriceMonitorApp(db)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
