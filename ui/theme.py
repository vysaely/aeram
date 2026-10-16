"""
AERAM — Complete Monochrome TUI-GUI Design System (QSS).
Pure pitch-black background, strictly achromatic palette, genuine character-grid alignment.

Palette Foundation:
- Main background:              #000000
- Secondary surfaces:           #080808
- Active / Inverted selection:  #E8E8E8
- Structural borders:           #353535
- Interior hairline dividers:   #202020
- Primary text:                 #E8E8E8
- Secondary text:               #A8A8A8
- Muted hints:                  #707070
- Disabled:                     #505050
- Inverted text (active/hover): #000000

Geometry:
- Strictly 0px border-radius across all interactive widgets for sharp character-cell alignment.
"""

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
CARET_SVG = (ASSETS_DIR / "tui_caret_down.svg").resolve().as_posix()
CARET_HOVER_SVG = (ASSETS_DIR / "tui_caret_down_hover.svg").resolve().as_posix()

DARK_THEME_QSS = f"""
/* Global Monospace Terminal Grid Base */
* {{
    font-family: "JetBrains Mono", "Fira Code", "Iosevka", "IBM Plex Mono", "Cascadia Code", "DejaVu Sans Mono", monospace;
    font-size: 11px;
}}

QMainWindow, QDialog {{
    background-color: #000000;
    color: #E8E8E8;
    border: none;
}}

QWidget {{
    background-color: transparent;
    color: #E8E8E8;
}}

/* Central Deck Frame */
QWidget#ramCentral {{
    background-color: #000000;
    border: 1px solid #353535;
}}

/* Header & Title Deck */
QFrame#top_header_frame {{
    background-color: #000000;
    border: none;
    border-bottom: 1px solid #353535;
}}

QFrame#tuiHeader {{
    background-color: #000000;
    border-bottom: 1px solid #353535;
}}

QLabel#tuiTitle {{
    font-size: 11px;
    font-weight: 700;
    color: #E8E8E8;
    letter-spacing: 1.2px;
}}

QLabel#tuiSubtitle {{
    font-size: 10px;
    color: #707070;
}}

/* Semantic Labels */
QLabel {{
    color: #A8A8A8;
    font-size: 10px;
    font-weight: 500;
}}

/* Input Fields & Text Editors */
QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background-color: #000000;
    border: 1px solid #353535;
    border-radius: 0px;
    padding: 2px 6px;
    color: #E8E8E8;
    selection-background-color: #E8E8E8;
    selection-color: #000000;
}}

QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QPlainTextEdit:hover, QTextEdit:hover {{
    border-color: #707070;
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid #E8E8E8;
    background-color: #050505;
    color: #FFFFFF;
}}

/* Monospace Combobox with Achromatic Caret */
QComboBox {{
    background-color: #000000;
    border: 1px solid #353535;
    border-radius: 0px;
    padding: 2px 6px;
    padding-right: 20px;
    color: #E8E8E8;
    selection-background-color: #E8E8E8;
    selection-color: #000000;
}}

QComboBox:hover {{
    border-color: #E8E8E8;
    background-color: #080808;
}}

QComboBox:focus {{
    border: 1px solid #E8E8E8;
    background-color: #000000;
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 20px;
    border: none;
}}

QComboBox::down-arrow {{
    width: 8px;
    height: 5px;
    image: url("{CARET_SVG}");
}}

QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{
    image: url("{CARET_HOVER_SVG}");
}}

QComboBox QAbstractItemView {{
    background-color: #000000;
    border: 1px solid #353535;
    border-radius: 0px;
    selection-background-color: #E8E8E8;
    selection-color: #000000;
    color: #E8E8E8;
    outline: none;
    padding: 3px;
}}

/* Achromatic Terminal Buttons */
QPushButton {{
    background-color: #000000;
    color: #E8E8E8;
    border: 1px solid #353535;
    border-radius: 0px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
    text-align: center;
    min-height: 18px;
}}

QPushButton[hasMenu="true"] {{
    padding-right: 22px;
    padding-left: 8px;
}}

QPushButton:hover {{
    background-color: #E8E8E8;
    border-color: #E8E8E8;
    color: #000000;
}}

QPushButton:pressed {{
    background-color: #A8A8A8;
    border-color: #A8A8A8;
    color: #000000;
}}

QPushButton:open {{
    background-color: #E8E8E8;
    border-color: #E8E8E8;
    color: #000000;
}}

QPushButton:disabled {{
    background-color: #000000;
    color: #505050;
    border-color: #202020;
}}

/* Dropdown Menu Carets */
QPushButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 8px;
    width: 8px;
    height: 5px;
    image: url("{CARET_SVG}");
}}

QPushButton::menu-indicator:hover,
QPushButton::menu-indicator:open {{
    image: url("{CARET_HOVER_SVG}");
}}

/* Compact Utility Buttons */
QPushButton#btn_bookmarks,
QPushButton#btn_replicate_alias {{
    padding: 4px 8px;
    background-color: #000000;
    border: 1px solid #353535;
    color: #E8E8E8;
}}

QPushButton#btn_bookmarks:hover,
QPushButton#btn_replicate_alias:hover {{
    background-color: #E8E8E8;
    border-color: #E8E8E8;
    color: #000000;
}}

/* Primary Inverted Terminal CTA (Join Server, Launch Server) */
QPushButton#primaryBtn, QPushButton#btnPrimary {{
    background-color: #E8E8E8;
    border: 1px solid #E8E8E8;
    color: #000000;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
    border-radius: 0px;
}}

QPushButton#primaryBtn:hover, QPushButton#btnPrimary:hover {{
    background-color: #FFFFFF;
    border-color: #FFFFFF;
    color: #000000;
}}

QPushButton#primaryBtn:pressed, QPushButton#btnPrimary:pressed {{
    background-color: #A8A8A8;
    border-color: #A8A8A8;
    color: #000000;
}}

/* Secondary Action Button */
QPushButton#btnAccent {{
    background-color: #000000;
    border: 1px solid #353535;
    color: #E8E8E8;
    border-radius: 0px;
}}

QPushButton#btnAccent:hover {{
    background-color: #E8E8E8;
    border-color: #E8E8E8;
    color: #000000;
}}

/* Danger / Cancel Button */
QPushButton#btnDanger {{
    background-color: #000000;
    border: 1px solid #606060;
    color: #E8E8E8;
    border-radius: 0px;
}}

QPushButton#btnDanger:hover {{
    background-color: #E8E8E8;
    border-color: #E8E8E8;
    color: #000000;
}}

/* Monospace Data Table */
QTableWidget {{
    background-color: #000000;
    alternate-background-color: #080808;
    color: #E8E8E8;
    gridline-color: transparent;
    border: 1px solid #353535;
    border-radius: 0px;
    selection-background-color: #E8E8E8;
    selection-color: #000000;
    outline: none;
}}

QTableWidget::item {{
    padding: 3px 6px;
    border: none;
    border-bottom: 1px solid #141414;
}}

QTableWidget::item:selected {{
    background-color: #E8E8E8;
    color: #000000;
}}

QTableWidget::item:hover:!selected {{
    background-color: #141414;
}}

QHeaderView::section {{
    background-color: #000000;
    color: #A8A8A8;
    padding: 5px 8px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
    border: none;
    border-right: 1px solid #202020;
    border-bottom: 1px solid #353535;
}}

QHeaderView::section:hover {{
    color: #FFFFFF;
    background-color: #141414;
}}

/* List Widget */
QListWidget {{
    background-color: #000000;
    border: 1px solid #353535;
    border-radius: 0px;
    color: #E8E8E8;
    padding: 2px;
}}

QListWidget::item {{
    padding: 4px 8px;
    border: none;
    border-bottom: 1px solid #141414;
}}

QListWidget::item:selected {{
    background-color: #E8E8E8;
    color: #000000;
}}

QListWidget::item:hover:!selected {{
    background-color: #141414;
}}

/* Character-Border GroupBox */
QGroupBox {{
    border: 1px solid #353535;
    border-radius: 0px;
    margin-top: 14px;
    padding: 10px;
    color: #E8E8E8;
    font-weight: 700;
    font-size: 10px;
    letter-spacing: 0.8px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    left: 8px;
    color: #E8E8E8;
}}

/* Checkbox with Sharp Terminal Indicator */
QCheckBox {{
    spacing: 6px;
    color: #A8A8A8;
    font-size: 11px;
}}

QCheckBox:hover {{
    color: #E8E8E8;
}}

QCheckBox::indicator {{
    width: 13px;
    height: 13px;
    border: 1px solid #606060;
    border-radius: 0px;
    background-color: #000000;
}}

QCheckBox::indicator:hover {{
    border-color: #E8E8E8;
}}

QCheckBox::indicator:checked {{
    background-color: #E8E8E8;
    border-color: #E8E8E8;
}}

/* Tab Widget */
QTabWidget::pane {{
    border: 1px solid #353535;
    background-color: #000000;
    border-radius: 0px;
}}

QTabBar::tab {{
    background-color: #000000;
    color: #A8A8A8;
    border: 1px solid #353535;
    padding: 5px 12px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.6px;
    border-radius: 0px;
    margin-right: 1px;
}}

QTabBar::tab:selected {{
    background-color: #E8E8E8;
    color: #000000;
    border-bottom: 1px solid #E8E8E8;
}}

QTabBar::tab:hover:!selected {{
    background-color: #141414;
    color: #E8E8E8;
}}

/* Precision Hairline Scrollbar */
QScrollBar:vertical {{
    border: none;
    background: #000000;
    width: 5px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: #353535;
    min-height: 24px;
    border-radius: 0px;
}}

QScrollBar::handle:vertical:hover {{
    background: #E8E8E8;
}}

QScrollBar:horizontal {{
    border: none;
    background: #000000;
    height: 5px;
    margin: 0px;
}}

QScrollBar::handle:horizontal {{
    background: #353535;
    min-width: 24px;
    border-radius: 0px;
}}

QScrollBar::handle:horizontal:hover {{
    background: #E8E8E8;
}}

QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
    border: none;
    height: 0px;
    width: 0px;
}}

/* Status Bar */
QStatusBar {{
    background-color: #000000;
    color: #A8A8A8;
    border-top: 1px solid #353535;
    font-size: 10px;
    padding: 0px 6px;
}}

QStatusBar::item {{
    border: none;
}}

QStatusBar QLabel {{
    color: #A8A8A8;
}}

/* Splitter */
QSplitter::handle {{
    background-color: #353535;
    width: 1px;
}}

/* Context Menus */
QMenu {{
    background-color: #000000;
    border: 1px solid #353535;
    color: #E8E8E8;
    padding: 3px 0px;
    border-radius: 0px;
}}

QMenu::item {{
    padding: 5px 22px 5px 14px;
    font-size: 11px;
}}

QMenu::item:selected {{
    background-color: #E8E8E8;
    color: #000000;
}}

QMenu::item:disabled {{
    color: #505050;
}}

QMenu::separator {{
    height: 1px;
    background-color: #202020;
    margin: 3px 6px;
}}

/* Progress Bar */
QProgressBar {{
    background-color: #000000;
    border: 1px solid #353535;
    border-radius: 0px;
    text-align: center;
    color: #E8E8E8;
    font-size: 10px;
    font-family: monospace;
}}

QProgressBar::chunk {{
    background-color: #E8E8E8;
}}
"""
