#!/usr/bin/env python3
"""CSFloat Mass Reprice — Modern PyQt6 GUI for bulk repricing CSFloat listings."""

import sys
import json
from pathlib import Path
from typing import Optional

import requests
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QScrollArea, QFrame, QCheckBox,
    QDoubleSpinBox, QProgressBar, QMessageBox, QStatusBar, QSizePolicy,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QRunnable, QThreadPool, QObject, pyqtSlot,
)
from PyQt6.QtGui import QPixmap, QImage, QFont, QColor

# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".csfloat_reprice.json"
BASE_URL    = "https://csfloat.com/api/v1"
STEAM_IMG   = "https://community.akamai.steamstatic.com/economy/image"

# Palette
C_BG       = "#0f1117"
C_SURF     = "#181926"
C_CARD     = "#1e2035"
C_CARD_SEL = "#222650"
C_BORDER   = "#2e3060"
C_BORDER_S = "#5865f2"
C_TEXT     = "#dde1f5"
C_MUTED    = "#6b6f9a"
C_PRIMARY  = "#5865f2"
C_PRI_H    = "#4752c4"
C_GREEN    = "#3ba55c"
C_RED      = "#ed4245"
C_GOLD     = "#f0a030"

RARITY_COLOR = {
    0: "#9da3b4", 1: "#9da3b4", 2: "#5e98d9",
    3: "#4b69ff", 4: "#8847ff", 5: "#d32ce6",
    6: "#eb4b4b", 7: "#e4ae39",
}

# Global image cache  icon_url -> QPixmap
_IMG_CACHE: dict[str, QPixmap] = {}


# ── Config ────────────────────────────────────────────────────────────────────

def load_cfg() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def save_cfg(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ── Background workers ────────────────────────────────────────────────────────

class StallWorker(QThread):
    done  = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, api_key: str, steam_id: str) -> None:
        super().__init__()
        self.api_key  = api_key
        self.steam_id = steam_id

    def run(self) -> None:
        try:
            r = requests.get(
                f"{BASE_URL}/users/{self.steam_id}/stall",
                headers={"Authorization": self.api_key},
                params={"limit": 1000, "sort_by": "highest_price"},
                timeout=30,
            )
            r.raise_for_status()
            self.done.emit(r.json().get("data", []))
        except requests.HTTPError as e:
            self.error.emit(f"HTTP {e.response.status_code}: {e.response.text[:300]}")
        except Exception as e:
            self.error.emit(str(e))


class _ImgSignals(QObject):
    loaded = pyqtSignal(str, QPixmap)


class ImgWorker(QRunnable):
    def __init__(self, icon_url: str) -> None:
        super().__init__()
        self.icon_url = icon_url
        self.signals  = _ImgSignals()
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self) -> None:
        url = self.icon_url
        if url in _IMG_CACHE:
            self.signals.loaded.emit(url, _IMG_CACHE[url])
            return
        try:
            full = url if url.startswith("http") else f"{STEAM_IMG}/{url}/64fx64f"
            r = requests.get(full, timeout=10)
            if r.ok:
                img = QImage()
                img.loadFromData(r.content)
                px = QPixmap.fromImage(img).scaled(
                    60, 60,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                _IMG_CACHE[url] = px
                self.signals.loaded.emit(url, px)
        except Exception:
            pass


class RepriceWorker(QThread):
    progress = pyqtSignal(int, int)
    done     = pyqtSignal(int, list)

    def __init__(self, api_key: str, changes: list[tuple[str, int]]) -> None:
        super().__init__()
        self.api_key = api_key
        self.changes = changes

    def run(self) -> None:
        hdrs = {"Authorization": self.api_key, "Content-Type": "application/json"}
        ok, errs = 0, []
        for i, (lid, price) in enumerate(self.changes):
            try:
                r = requests.patch(
                    f"{BASE_URL}/listings/{lid}",
                    headers=hdrs,
                    json={"price": price},
                    timeout=15,
                )
                if r.ok:
                    ok += 1
                else:
                    errs.append(f"[{lid}] HTTP {r.status_code}: {r.text[:120]}")
            except Exception as e:
                errs.append(f"[{lid}] {e}")
            self.progress.emit(i + 1, len(self.changes))
        self.done.emit(ok, errs)


# ── Item row widget ───────────────────────────────────────────────────────────

class ItemRow(QFrame):
    toggled = pyqtSignal()

    _BASE = (
        f"QFrame#itemRow{{"
        f"background:{C_CARD};border:1px solid {C_BORDER};border-radius:8px;}}"
    )
    _SEL = (
        f"QFrame#itemRow{{"
        f"background:{C_CARD_SEL};border:1px solid {C_BORDER_S};border-radius:8px;}}"
    )

    def __init__(self, listing: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("itemRow")
        self.listing    = listing
        self.item       = listing.get("item", {})
        self.listing_id = listing["id"]
        self.cur_price  = listing["price"]   # cents
        self.new_price: Optional[int] = None
        self._build()
        self._start_img()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setStyleSheet(self._BASE)
        self.setFixedHeight(80)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 8, 14, 8)
        root.setSpacing(10)

        # Checkbox
        self.cb = QCheckBox()
        self.cb.setChecked(True)
        self.cb.stateChanged.connect(self._on_toggle)
        root.addWidget(self.cb)

        # Thumbnail
        self.thumb = QLabel()
        self.thumb.setFixedSize(60, 60)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            f"background:{C_SURF};border-radius:4px;color:{C_MUTED};font-size:9px;"
        )
        self.thumb.setText("···")
        root.addWidget(self.thumb)

        # ── Info column ───────────────────────────────────────────────────────
        info = QVBoxLayout()
        info.setSpacing(3)
        info.setContentsMargins(0, 0, 0, 0)

        # Name row
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_row.setContentsMargins(0, 0, 0, 0)

        name_txt = self.item.get("market_hash_name", "Unknown Item")
        rarity   = self.item.get("rarity", 0)
        col      = RARITY_COLOR.get(rarity, C_TEXT)

        name_lbl = QLabel(name_txt)
        name_lbl.setStyleSheet(f"color:{col};font-weight:700;font-size:13px;")
        name_row.addWidget(name_lbl)

        if self.item.get("is_stattrak"):
            tag = self._badge("StatTrak™", "#cf6a32", "#2e1c0c")
            name_row.addWidget(tag)
        if self.item.get("is_souvenir"):
            tag = self._badge("Souvenir", C_GOLD, "#2a1e00")
            name_row.addWidget(tag)

        # Low rank badge
        lr = self.item.get("low_rank")
        if lr and lr <= 100:
            tag = self._badge(f"#{lr}", "#44cfb2", "#0a2520")
            name_row.addWidget(tag)

        name_row.addStretch()
        info.addLayout(name_row)

        # Float · Pattern · Wear
        tags: list[str] = []
        fv = self.item.get("float_value")
        if fv is not None:
            tags.append(f"Float {fv:.6f}")
        ps = self.item.get("paint_seed")
        if ps is not None:
            tags.append(f"Pattern {ps}")
        wear = self.item.get("wear_name")
        if wear:
            tags.append(wear)
        if tags:
            t = QLabel("  ·  ".join(tags))
            t.setStyleSheet(f"color:{C_MUTED};font-size:11px;")
            info.addWidget(t)

        # Stickers & keychains
        stickers  = self.item.get("stickers")  or []
        keychains = self.item.get("keychains") or []
        extras: list[str] = []
        if stickers:
            extras.append("Stickers: " + ", ".join(s.get("name", "?") for s in stickers))
        if keychains:
            extras.append("Keychain: " + ", ".join(k.get("name", "?") for k in keychains))
        if extras:
            e = QLabel("  ·  ".join(extras))
            e.setStyleSheet(f"color:{C_GOLD};font-size:11px;")
            e.setWordWrap(False)
            info.addWidget(e)

        root.addLayout(info, stretch=1)

        # ── Price column ──────────────────────────────────────────────────────
        pcol = QVBoxLayout()
        pcol.setSpacing(1)
        pcol.setContentsMargins(0, 0, 0, 0)
        pcol.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        ref = self.listing.get("reference", {})
        ref_p = ref.get("predicted_price") or ref.get("base_price")
        if ref_p:
            rl = QLabel(f"ref ${ref_p / 100:,.2f}")
            rl.setStyleSheet(f"color:{C_MUTED};font-size:10px;")
            rl.setAlignment(Qt.AlignmentFlag.AlignRight)
            pcol.addWidget(rl)

        cur_lbl = QLabel(f"${self.cur_price / 100:,.2f}")
        cur_lbl.setStyleSheet(f"color:{C_TEXT};font-size:15px;font-weight:700;")
        cur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        pcol.addWidget(cur_lbl)

        arrow = QLabel("↓")
        arrow.setStyleSheet(f"color:{C_MUTED};font-size:10px;")
        arrow.setAlignment(Qt.AlignmentFlag.AlignRight)
        pcol.addWidget(arrow)

        self.new_lbl = QLabel("—")
        self.new_lbl.setStyleSheet(f"color:{C_MUTED};font-size:15px;font-weight:700;")
        self.new_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.new_lbl.setMinimumWidth(100)
        pcol.addWidget(self.new_lbl)

        root.addLayout(pcol)

    @staticmethod
    def _badge(text: str, fg: str, bg: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{fg};background:{bg};border-radius:3px;"
            f"padding:1px 6px;font-size:10px;font-weight:600;"
        )
        return lbl

    # ── image ─────────────────────────────────────────────────────────────────

    def _start_img(self) -> None:
        icon_url = self.item.get("icon_url", "")
        if not icon_url:
            return
        if icon_url in _IMG_CACHE:
            self.thumb.setText("")
            self.thumb.setPixmap(_IMG_CACHE[icon_url])
            return
        w = ImgWorker(icon_url)
        w.signals.loaded.connect(self._on_img)
        QThreadPool.globalInstance().start(w)

    def _on_img(self, _url: str, px: QPixmap) -> None:
        self.thumb.setText("")
        self.thumb.setPixmap(px)

    # ── interaction ───────────────────────────────────────────────────────────

    def _on_toggle(self) -> None:
        self._refresh_style()
        self.toggled.emit()

    def _refresh_style(self) -> None:
        self.setStyleSheet(self._SEL if self.cb.isChecked() else self._BASE)

    def is_selected(self) -> bool:
        return self.cb.isChecked()

    def set_selected(self, v: bool) -> None:
        self.cb.blockSignals(True)
        self.cb.setChecked(v)
        self.cb.blockSignals(False)
        self._refresh_style()

    def update_preview(self, pct: float) -> None:
        if pct == 0.0:
            self.new_price = None
            self.new_lbl.setText("—")
            self.new_lbl.setStyleSheet(f"color:{C_MUTED};font-size:15px;font-weight:700;")
            return
        raw = int(round(self.cur_price * (1 + pct / 100)))
        self.new_price = max(1, raw)
        color = C_GREEN if self.new_price >= self.cur_price else C_RED
        self.new_lbl.setText(f"${self.new_price / 100:,.2f}")
        self.new_lbl.setStyleSheet(f"color:{color};font-size:15px;font-weight:700;")


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._cfg      = load_cfg()
        self._rows: list[ItemRow] = []
        self._stall_w:   Optional[StallWorker]   = None
        self._reprice_w: Optional[RepriceWorker] = None

        QThreadPool.globalInstance().setMaxThreadCount(12)

        self.setWindowTitle("CSFloat Mass Reprice")
        self.resize(1300, 840)
        self.setMinimumSize(900, 600)

        self._build_ui()
        self._restore()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        vbox.addWidget(self._mk_header())
        vbox.addWidget(self._mk_subbar())
        vbox.addWidget(self._mk_list(), stretch=1)
        vbox.addWidget(self._mk_bottom())

        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage(
            "Enter your CSFloat API key and Steam ID, then click  Load Stall."
        )

    def _mk_header(self) -> QWidget:
        f = QFrame()
        f.setObjectName("hdr")
        f.setFixedHeight(54)
        f.setStyleSheet(
            f"QFrame#hdr{{background:{C_SURF};border-bottom:1px solid {C_BORDER};}}"
        )
        lay = QHBoxLayout(f)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(10)

        title = QLabel("CSFloat  Mass Reprice")
        title.setStyleSheet(
            f"color:{C_TEXT};font-size:17px;font-weight:800;letter-spacing:0.5px;"
        )
        lay.addWidget(title)
        lay.addStretch()

        # API key
        lay.addWidget(self._muted_label("API key"))
        self.api_edit = QLineEdit()
        self.api_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_edit.setPlaceholderText("CSFloat API key")
        self.api_edit.setFixedSize(230, 34)
        self.api_edit.editingFinished.connect(self._save)
        lay.addWidget(self.api_edit)

        # Steam ID
        lay.addWidget(self._muted_label("Steam ID"))
        self.steam_edit = QLineEdit()
        self.steam_edit.setPlaceholderText("76561198…")
        self.steam_edit.setFixedSize(160, 34)
        self.steam_edit.editingFinished.connect(self._save)
        lay.addWidget(self.steam_edit)

        # Load button
        self.load_btn = QPushButton("  Load Stall")
        self.load_btn.setFixedHeight(34)
        self.load_btn.clicked.connect(self._load)
        lay.addWidget(self.load_btn)

        return f

    def _mk_subbar(self) -> QWidget:
        f = QFrame()
        f.setObjectName("sub")
        f.setFixedHeight(38)
        f.setStyleSheet(
            f"QFrame#sub{{background:{C_SURF};border-bottom:1px solid {C_BORDER};}}"
        )
        lay = QHBoxLayout(f)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(8)

        self.info_lbl = QLabel("No items loaded.")
        self.info_lbl.setStyleSheet(f"color:{C_MUTED};font-size:12px;")
        lay.addWidget(self.info_lbl)
        lay.addStretch()

        self.sel_all_btn = self._sec_btn("Select all", self._sel_all)
        self.desel_btn   = self._sec_btn("Deselect all", self._desel_all)
        self.inv_btn     = self._sec_btn("Invert", self._invert)
        for b in (self.sel_all_btn, self.desel_btn, self.inv_btn):
            b.setFixedHeight(26)
            lay.addWidget(b)

        return f

    def _mk_list(self) -> QWidget:
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll.setStyleSheet(
            f"QScrollArea{{background:{C_BG};border:none;}}"
            f"QScrollArea>QWidget>QWidget{{background:{C_BG};}}"
        )

        self._list_w = QWidget()
        self._list_w.setStyleSheet(f"background:{C_BG};")
        self._list_lay = QVBoxLayout(self._list_w)
        self._list_lay.setContentsMargins(12, 10, 12, 10)
        self._list_lay.setSpacing(4)
        self._list_lay.addStretch()

        self.scroll.setWidget(self._list_w)
        return self.scroll

    def _mk_bottom(self) -> QWidget:
        f = QFrame()
        f.setObjectName("bot")
        f.setFixedHeight(62)
        f.setStyleSheet(
            f"QFrame#bot{{background:{C_SURF};border-top:1px solid {C_BORDER};}}"
        )
        lay = QHBoxLayout(f)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(10)

        lay.addWidget(self._muted_label("Adjust price:"))

        self.pct_spin = QDoubleSpinBox()
        self.pct_spin.setRange(-99.0, 9999.0)
        self.pct_spin.setDecimals(2)
        self.pct_spin.setSuffix("  %")
        self.pct_spin.setValue(0.0)
        self.pct_spin.setSingleStep(1.0)
        self.pct_spin.setFixedSize(120, 40)
        self.pct_spin.valueChanged.connect(self._preview)
        lay.addWidget(self.pct_spin)

        for val in (-10, -5, -1, 1, 5, 10):
            sign = "+" if val > 0 else ""
            b = QPushButton(f"{sign}{val}%")
            b.setObjectName("btnSecondary")
            b.setFixedHeight(32)
            b.clicked.connect(lambda _, v=val: self.pct_spin.setValue(v))
            lay.addWidget(b)

        lay.addStretch()

        self.prog = QProgressBar()
        self.prog.setFixedSize(200, 8)
        self.prog.setVisible(False)
        lay.addWidget(self.prog)

        self.sel_lbl = QLabel("0 selected")
        self.sel_lbl.setStyleSheet(f"color:{C_MUTED};min-width:90px;")
        self.sel_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        lay.addWidget(self.sel_lbl)

        self.apply_btn = QPushButton("Apply Reprice")
        self.apply_btn.setObjectName("btnSuccess")
        self.apply_btn.setFixedHeight(40)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        lay.addWidget(self.apply_btn)

        return f

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _muted_label(txt: str) -> QLabel:
        l = QLabel(txt)
        l.setStyleSheet(f"color:{C_MUTED};font-size:12px;")
        return l

    @staticmethod
    def _sec_btn(txt: str, slot) -> QPushButton:
        b = QPushButton(txt)
        b.setObjectName("btnSecondary")
        b.clicked.connect(slot)
        return b

    # ── logic ─────────────────────────────────────────────────────────────────

    def _restore(self) -> None:
        self.api_edit.setText(self._cfg.get("api_key", ""))
        self.steam_edit.setText(self._cfg.get("steam_id", ""))

    def _save(self) -> None:
        self._cfg["api_key"]  = self.api_edit.text().strip()
        self._cfg["steam_id"] = self.steam_edit.text().strip()
        save_cfg(self._cfg)

    def _load(self) -> None:
        api = self.api_edit.text().strip()
        sid = self.steam_edit.text().strip()
        if not api or not sid:
            QMessageBox.warning(self, "Missing", "API key and Steam ID are both required.")
            return
        self._save()
        self.load_btn.setEnabled(False)
        self.load_btn.setText("Loading…")
        self.statusbar.showMessage("Loading stall…")
        self._clear()

        self._stall_w = StallWorker(api, sid)
        self._stall_w.done.connect(self._on_loaded)
        self._stall_w.error.connect(self._on_load_err)
        self._stall_w.start()

    def _on_loaded(self, listings: list) -> None:
        self.load_btn.setEnabled(True)
        self.load_btn.setText("Reload")
        for lst in listings:
            row = ItemRow(lst)
            row.toggled.connect(self._update_stats)
            self._list_lay.insertWidget(self._list_lay.count() - 1, row)
            self._rows.append(row)
        self._preview(self.pct_spin.value())
        self._update_stats()
        self.statusbar.showMessage(f"Loaded {len(listings)} listing(s).")

    def _on_load_err(self, msg: str) -> None:
        self.load_btn.setEnabled(True)
        self.load_btn.setText("Load Stall")
        self.statusbar.showMessage(f"Error: {msg}")
        QMessageBox.critical(self, "Load failed", msg)

    def _clear(self) -> None:
        for row in self._rows:
            self._list_lay.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        self._update_stats()

    def _preview(self, pct: float) -> None:
        for row in self._rows:
            row.update_preview(pct)
        self._update_stats()

    def _update_stats(self) -> None:
        n  = len(self._rows)
        ns = sum(1 for r in self._rows if r.is_selected())
        self.info_lbl.setText(
            f"{n} item{'s' if n != 1 else ''}  ·  {ns} selected"
        )
        self.sel_lbl.setText(f"{ns} selected")
        self.apply_btn.setEnabled(ns > 0 and self.pct_spin.value() != 0.0)

    def _sel_all(self) -> None:
        for r in self._rows:
            r.set_selected(True)
        self._update_stats()

    def _desel_all(self) -> None:
        for r in self._rows:
            r.set_selected(False)
        self._update_stats()

    def _invert(self) -> None:
        for r in self._rows:
            r.set_selected(not r.is_selected())
        self._update_stats()

    def _apply(self) -> None:
        pct = self.pct_spin.value()
        changes = [
            (r.listing_id, r.new_price)
            for r in self._rows
            if r.is_selected() and r.new_price is not None
        ]
        if not changes:
            return

        ans = QMessageBox.question(
            self,
            "Confirm reprice",
            f"Apply {pct:+.2f}% to {len(changes)} item(s)?\n\n"
            "This will update the prices on CSFloat.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        self.apply_btn.setEnabled(False)
        self.load_btn.setEnabled(False)
        self.prog.setMaximum(len(changes))
        self.prog.setValue(0)
        self.prog.setVisible(True)
        self.statusbar.showMessage(f"Repricing {len(changes)} item(s)…")

        self._reprice_w = RepriceWorker(self.api_edit.text().strip(), changes)
        self._reprice_w.progress.connect(lambda d, _t: self.prog.setValue(d))
        self._reprice_w.done.connect(self._on_reprice_done)
        self._reprice_w.start()

    def _on_reprice_done(self, ok: int, errs: list) -> None:
        self.load_btn.setEnabled(True)
        self.apply_btn.setEnabled(True)
        self.prog.setVisible(False)

        if errs:
            detail = "\n".join(errs[:20])
            if len(errs) > 20:
                detail += f"\n…and {len(errs) - 20} more"
            QMessageBox.warning(
                self,
                "Completed with errors",
                f"{ok} repriced successfully, {len(errs)} failed:\n\n{detail}",
            )
        else:
            QMessageBox.information(
                self, "Done", f"{ok} item(s) repriced successfully!"
            )

        self.statusbar.showMessage(
            f"Done — {ok} repriced, {len(errs)} failed."
        )
        if ok > 0:
            self._load()


# ── Application stylesheet ────────────────────────────────────────────────────

STYLE = f"""
* {{
    font-family: "Segoe UI", "SF Pro Display", "Inter", sans-serif;
}}
QWidget {{
    background: {C_BG};
    color: {C_TEXT};
    font-size: 13px;
}}
QLabel {{
    background: transparent;
}}
QCheckBox {{
    background: transparent;
}}
QScrollArea,
QScrollArea > QWidget > QWidget {{
    background: {C_BG};
    border: none;
}}
QScrollBar:vertical {{
    background: {C_SURF};
    width: 7px;
    border-radius: 3px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {C_BORDER};
    border-radius: 3px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {C_PRIMARY};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: none;
}}

QPushButton {{
    background: {C_PRIMARY};
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 0 20px;
    font-weight: 600;
    font-size: 13px;
}}
QPushButton:hover  {{ background: {C_PRI_H}; }}
QPushButton:pressed {{ background: #3b43a8; }}
QPushButton:disabled {{ background: {C_SURF}; color: {C_MUTED}; }}

QPushButton#btnSecondary {{
    background: {C_SURF};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
}}
QPushButton#btnSecondary:hover {{
    background: {C_CARD};
    border-color: {C_PRIMARY};
}}

QPushButton#btnSuccess {{
    background: #245e35;
    color: #ffffff;
}}
QPushButton#btnSuccess:hover  {{ background: {C_GREEN}; }}
QPushButton#btnSuccess:disabled {{ background: {C_SURF}; color: {C_MUTED}; }}

QLineEdit {{
    background: {C_CARD};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    padding: 5px 10px;
    selection-background-color: {C_PRIMARY};
}}
QLineEdit:focus {{ border-color: {C_PRIMARY}; }}

QDoubleSpinBox {{
    background: {C_CARD};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {C_PRIMARY};
}}
QDoubleSpinBox:focus {{ border-color: {C_PRIMARY}; }}
QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {{
    background: {C_BORDER};
    border: none;
    width: 18px;
}}
QDoubleSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover {{
    background: {C_PRIMARY};
}}

QProgressBar {{
    background: {C_CARD};
    border: none;
    border-radius: 4px;
}}
QProgressBar::chunk {{
    background: {C_PRIMARY};
    border-radius: 4px;
}}

QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border: 2px solid {C_BORDER};
    border-radius: 4px;
    background: {C_CARD};
}}
QCheckBox::indicator:checked {{
    background: {C_PRIMARY};
    border-color: {C_PRIMARY};
}}
QCheckBox::indicator:hover {{
    border-color: {C_PRIMARY};
}}

QStatusBar {{
    background: {C_SURF};
    color: {C_MUTED};
    border-top: 1px solid {C_BORDER};
    font-size: 12px;
}}

QMessageBox {{
    background: {C_SURF};
}}
QMessageBox QLabel {{
    color: {C_TEXT};
}}
QMessageBox QPushButton {{
    min-width: 80px;
    padding: 6px 16px;
}}
"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)

    win = MainWindow()
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
