# -*- coding: utf-8 -*-
"""Native LanceDB knowledge browser with lazy semantic graph loading."""

from __future__ import annotations

import argparse
import faulthandler
import json
import math
import os
import sqlite3
import sys
import traceback

import numpy as np
from PySide6.QtCore import QCoreApplication, QLineF, QObject, QPointF, QRunnable, Qt, QThreadPool, Signal, Slot, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QGuiApplication, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from knowledge_browser_core import KnowledgeBrowserData, RELATION_TYPES, RelationStore
from content_graph import ContentGraphStore, extractor_from_environment
from knowledge_graph import build_graphs


def resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    if getattr(sys, "frozen", False):
        executable_dir = os.path.dirname(sys.executable)
        project_dir = os.path.abspath(os.path.join(executable_dir, "..", ".."))
        if os.path.isfile(os.path.join(project_dir, "kb-config.json")):
            return project_dir
        return executable_dir
    return resource_dir()


RESOURCE_DIR = resource_dir()
DEFAULT_CONFIG = os.path.join(data_dir(), "kb-config.json")
if not os.path.isfile(DEFAULT_CONFIG):
    DEFAULT_CONFIG = os.path.join(RESOURCE_DIR, "kb-config.json")
DEFAULT_RELATIONS = os.path.join(data_dir(), "knowledge_graph_relations.sqlite3")
DEFAULT_CONTENT_GRAPH = os.path.join(data_dir(), "content_graph.sqlite3")
DEFAULT_LOG = os.path.join(data_dir(), "knowledge_browser.log")
ALL_BASES_SCOPE = "__all__"
BASE_COLORS = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#db2777", "#0891b2"]
RELATION_LABELS = {
    "related": "相关",
    "supports": "支持",
    "contradicts": "冲突",
    "depends_on": "依赖",
    "supersedes": "替代",
}
RELATION_COLORS = {
    "related": "#7c3aed",
    "supports": "#16a34a",
    "contradicts": "#dc2626",
    "depends_on": "#d97706",
    "supersedes": "#0891b2",
}
CONTENT_NODE_COLORS = {
    "document": "#2563eb",
    "chunk": "#94a3b8",
    "entity": "#9333ea",
    "claim": "#d97706",
    "method": "#16a34a",
    "topic": "#0891b2",
    "dataset": "#db2777",
}
CONTENT_EDGE_COLORS = {
    "contains": "#cbd5e1",
    "mentions": "#a78bfa",
    "asserts": "#f59e0b",
    "about": "#fbbf24",
    "uses": "#22c55e",
    "supports": "#16a34a",
    "contradicts": "#dc2626",
    "depends_on": "#d97706",
    "supersedes": "#0891b2",
    "similar_to": "#64748b",
    "belongs_to": "#06b6d4",
    "evolves": "#0f766e",
}
_FAULT_LOG_STREAM = None


def append_log(message: str):
    try:
        with open(DEFAULT_LOG, "a", encoding="utf-8") as stream:
            stream.write(message.rstrip() + "\n")
    except OSError:
        pass


def install_crash_logging():
    """Persist Python and native crash diagnostics for the windowed executable."""
    global _FAULT_LOG_STREAM
    try:
        _FAULT_LOG_STREAM = open(DEFAULT_LOG, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(_FAULT_LOG_STREAM, all_threads=True)
    except OSError:
        _FAULT_LOG_STREAM = None

    def exception_hook(exception_type, exception, traceback_object):
        details = "".join(traceback.format_exception(exception_type, exception, traceback_object))
        append_log(details)
        sys.__excepthook__(exception_type, exception, traceback_object)

    sys.excepthook = exception_hook


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.result.emit(self.function())
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class GraphView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self._auto_fit = True
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_graph_now)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor("#f4f7fb"))

    def wheelEvent(self, event):
        self._auto_fit = False
        factor = 1.18 if event.angleDelta().y() > 0 else 1 / 1.18
        next_scale = self.transform().m11() * factor
        if 0.08 <= next_scale <= 8:
            self.scale(factor, factor)

    def fit_graph(self):
        self._auto_fit = True
        self._fit_graph_now()

    def _fit_graph_now(self):
        rect = self.scene().itemsBoundingRect()
        if not rect.isNull():
            self.fitInView(rect.adjusted(-70, -70, 70, 70), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._auto_fit:
            self._fit_timer.start(50)


class NodeItem(QGraphicsEllipseItem):
    def __init__(self, node, position, color):
        radius = float(node["value"])
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.node = node
        self.setPos(*position)
        self.setBrush(QColor(color))
        self.setPen(QPen(QColor("#ffffff"), 2))
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable)
        self.setToolTip(f"[{node['knowledge_base']}] {node['source']}")
        label = QGraphicsSimpleTextItem(node["label"], self)
        label.setBrush(QColor("#172033"))
        label.setFont(QFont("Microsoft YaHei", 8))
        rect = label.boundingRect()
        label.setPos(-rect.width() / 2, radius + 5)


class ContentNodeItem(QGraphicsEllipseItem):
    def __init__(self, node, position):
        radius_by_kind = {
            "document": 20, "chunk": 8, "entity": 13, "claim": 15,
            "method": 14, "topic": 17, "dataset": 14,
        }
        radius = radius_by_kind.get(node.get("kind"), 11)
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.node = node
        self.setPos(*position)
        self.setBrush(QColor(CONTENT_NODE_COLORS.get(node.get("kind"), "#64748b")))
        self.setPen(QPen(QColor("#ffffff"), 2))
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable)
        self.setToolTip(f"{node.get('kind', '')}: {node.get('label', '')}")
        label_text = node.get("label", "")
        if len(label_text) > 46:
            label_text = label_text[:45] + "…"
        label = QGraphicsSimpleTextItem(label_text, self)
        label.setBrush(QColor("#172033"))
        label.setFont(QFont("Microsoft YaHei", 8))
        rect = label.boundingRect()
        label.setPos(-rect.width() / 2, radius + 4)


class ContentEdgeItem(QGraphicsLineItem):
    def __init__(self, edge, line):
        super().__init__(line)
        self.edge = edge
        relation = edge.get("relation_type", "")
        color = QColor(CONTENT_EDGE_COLORS.get(relation, "#94a3b8"))
        width = 2.2 if relation in {"supports", "contradicts", "supersedes", "evolves"} else 1.2
        self.setPen(QPen(color, width))
        self.setZValue(-2)
        self.setFlag(QGraphicsLineItem.GraphicsItemFlag.ItemIsSelectable)
        self.setToolTip(
            f"{relation} · 置信度 {float(edge.get('confidence', 0)):.2f} · "
            f"证据 Chunk #{edge.get('evidence_chunk_index')}"
        )


def force_layout(graph):
    nodes = graph["nodes"]
    if not nodes:
        return np.empty((0, 2))
    rng = np.random.default_rng(20260712)
    positions = rng.uniform(-350, 350, (len(nodes), 2)).astype(np.float64)
    index_by_id = {node["id"]: index for index, node in enumerate(nodes)}
    pairs = [(index_by_id[e["from"]], index_by_id[e["to"]]) for e in graph["edges"]
             if e["from"] in index_by_id and e["to"] in index_by_id]
    ideal = max(95.0, 900.0 / np.sqrt(len(nodes)))
    for iteration in range(120):
        delta = positions[:, None, :] - positions[None, :, :]
        distance = np.linalg.norm(delta, axis=2)
        np.fill_diagonal(distance, 1.0)
        displacement = ((delta / distance[:, :, None]) * (ideal ** 2 / distance)[:, :, None]).sum(axis=1)
        for left, right in pairs:
            vector = positions[left] - positions[right]
            length = max(float(np.linalg.norm(vector)), 1.0)
            movement = vector / length * (length ** 2 / ideal)
            displacement[left] -= movement
            displacement[right] += movement
        temperature = 34 * (1 - iteration / 120) + 2
        magnitude = np.linalg.norm(displacement, axis=1)
        positions += displacement / np.maximum(magnitude[:, None], 1.0) * np.minimum(magnitude, temperature)[:, None]
    return positions


class RelationDialog(QDialog):
    def __init__(self, parent, documents, relation=None, evidence=None):
        super().__init__(parent)
        self.setWindowTitle("编辑人工关系" if relation else "新建人工关系")
        layout = QFormLayout(self)
        self.target = QComboBox()
        for document in documents:
            self.target.addItem(document["source"], document["id"])
        self.target.setEnabled(relation is None)
        self.kind = QComboBox()
        for value in RELATION_TYPES:
            self.kind.addItem(f"{RELATION_LABELS[value]} ({value})", value)
        self.note = QTextEdit()
        self.note.setMaximumHeight(100)
        self.bind_evidence = QCheckBox("绑定当前 Chunk 作为证据")
        self.evidence = evidence
        self.bind_evidence.setEnabled(evidence is not None)
        if relation:
            self.kind.setCurrentIndex(max(0, self.kind.findData(relation["relation_type"])))
            self.note.setPlainText(relation.get("note", ""))
            self.bind_evidence.setChecked(relation.get("evidence_chunk_index") is not None)
        layout.addRow("目标文档：", self.target)
        layout.addRow("关系类型：", self.kind)
        layout.addRow("备注：", self.note)
        layout.addRow("", self.bind_evidence)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self):
        evidence_source, evidence_index = "", None
        if self.bind_evidence.isChecked() and self.evidence:
            evidence_source, evidence_index = self.evidence
        return {
            "target_id": self.target.currentData(),
            "relation_type": self.kind.currentData(),
            "note": self.note.toPlainText().strip(),
            "evidence_source": evidence_source,
            "evidence_chunk_index": evidence_index,
        }


class KnowledgeBrowserWindow(QMainWindow):
    def __init__(self, config_path: str, relations_path: str, content_graph_path: str | None = None):
        super().__init__()
        self.config_path = config_path
        self.data = KnowledgeBrowserData(config_path)
        self.relations = RelationStore(relations_path)
        self.content_graph_store = ContentGraphStore(content_graph_path or DEFAULT_CONTENT_GRAPH)
        self.relations.remap_knowledge_bases(self.data.aliases)
        self.content_graph_store.remap_knowledge_bases(self.data.aliases)
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)
        self.chunk_pool = QThreadPool(self)
        self.chunk_pool.setMaxThreadCount(1)
        self._workers = set()
        self._chunk_workers = set()
        self.documents = []
        self.visible_documents = []
        self.current_document = None
        self.current_chunks = []
        self.current_chunk_row = -1
        self.search_generation = 0
        self.document_generation = 0
        self.chunk_generation = 0
        self.graph_generation = 0
        self.content_graph_generation = 0
        self.graph_loaded_for = ""
        self.health_loaded_for = ""
        self.graph = {"nodes": [], "edges": [], "chunks": 0, "knowledge_bases": []}
        self.content_graph = {"nodes": [], "edges": [], "document_id": None}
        self.node_items = {}
        self.content_node_items = {}
        self.current_content_evidence = None
        self.pending_source_jump = None
        self.last_error = None
        self.setWindowTitle("LanceDB 知识浏览器")
        self.resize(1600, 940)
        self._build_ui()
        self._populate_bases()
        if self.kb_combo.count():
            self.load_documents()

    def _run(self, function, on_result, on_error=None, pool=None):
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(on_error or self._show_background_error)
        worker.signals.finished.connect(lambda current=worker: self._workers.discard(current))
        (pool or self.thread_pool).start(worker)
        return worker

    def _run_latest_chunk_task(self, function, on_result):
        for worker in list(self._chunk_workers):
            if self.chunk_pool.tryTake(worker):
                self._chunk_workers.discard(worker)
                self._workers.discard(worker)
        worker = self._run(function, on_result, pool=self.chunk_pool)
        self._chunk_workers.add(worker)
        worker.signals.finished.connect(lambda current=worker: self._chunk_workers.discard(current))

    def _show_background_error(self, details):
        self.last_error = details
        append_log(details)
        QMessageBox.critical(self, "后台任务失败", details.splitlines()[-1] if details else "未知错误")

    def _build_ui(self):
        toolbar = QToolBar("搜索工具")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addWidget(QLabel("全局搜索："))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("输入问题或关键词")
        self.search_box.setMinimumWidth(360)
        self.search_box.returnPressed.connect(self.run_search)
        toolbar.addWidget(self.search_box)
        self.search_mode = QComboBox()
        self.search_mode.addItem("Vector", "vector")
        self.search_mode.addItem("Text", "text")
        self.search_mode.addItem("Hybrid", "hybrid")
        toolbar.addWidget(self.search_mode)
        self.reranker = QCheckBox("Reranker")
        self.reranker.setChecked(True)
        toolbar.addWidget(self.reranker)
        self.search_source = QLineEdit()
        self.search_source.setPlaceholderText("来源过滤")
        self.search_source.setMaximumWidth(150)
        toolbar.addWidget(self.search_source)
        self.search_category = QLineEdit()
        self.search_category.setPlaceholderText("类别过滤")
        self.search_category.setMaximumWidth(120)
        toolbar.addWidget(self.search_category)
        search_action = QAction("搜索", self)
        search_action.triggered.connect(self.run_search)
        toolbar.addAction(search_action)
        refresh_action = QAction("刷新", self)
        refresh_action.triggered.connect(self.refresh_current)
        toolbar.addAction(refresh_action)

        self.main_tabs = QTabWidget()
        self.main_tabs.currentChanged.connect(self._main_tab_changed)
        self.setCentralWidget(self.main_tabs)
        self.main_tabs.addTab(self._build_browser_tab(), "文件浏览器")
        self.main_tabs.addTab(self._build_graph_tab(), "文档关系图（旧）")
        self.main_tabs.addTab(self._build_content_graph_tab(), "内容图谱（实体/观点）")

    def _build_browser_tab(self):
        container = QWidget()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("知识库"))
        self.kb_combo = QComboBox()
        self.kb_combo.currentIndexChanged.connect(self.load_documents)
        left_layout.addWidget(self.kb_combo)
        self.document_filter = QLineEdit()
        self.document_filter.setPlaceholderText("快速过滤文件名/路径")
        self.document_filter.textChanged.connect(self.apply_document_filters)
        left_layout.addWidget(self.document_filter)
        filter_row = QHBoxLayout()
        self.category_filter = QComboBox()
        self.category_filter.addItem("全部类别", "")
        self.category_filter.currentIndexChanged.connect(self.apply_document_filters)
        self.extension_filter = QComboBox()
        self.extension_filter.addItem("全部类型", "")
        self.extension_filter.currentIndexChanged.connect(self.apply_document_filters)
        filter_row.addWidget(self.category_filter)
        filter_row.addWidget(self.extension_filter)
        left_layout.addLayout(filter_row)
        self.document_table = QTableWidget(0, 5)
        self.document_table.setHorizontalHeaderLabels(["文件", "Chunk", "类别", "类型", "原件"])
        self.document_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.document_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.document_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.document_table.verticalHeader().hide()
        self.document_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.document_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.document_table.itemSelectionChanged.connect(self.select_document)
        left_layout.addWidget(self.document_table)
        self.document_summary = QLabel("正在加载…")
        left_layout.addWidget(self.document_summary)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        self.document_title = QLabel("请选择文件")
        self.document_title.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.document_title.setWordWrap(True)
        center_layout.addWidget(self.document_title)
        source_buttons = QHBoxLayout()
        self.open_source_button = QPushButton("打开原文件")
        self.open_source_button.clicked.connect(self.open_source)
        self.copy_source_button = QPushButton("复制来源路径")
        self.copy_source_button.clicked.connect(self.copy_source)
        self.open_source_button.setEnabled(False)
        self.copy_source_button.setEnabled(False)
        source_buttons.addWidget(self.open_source_button)
        source_buttons.addWidget(self.copy_source_button)
        source_buttons.addStretch()
        center_layout.addLayout(source_buttons)
        self.content_tabs = QTabWidget()
        self.content_tabs.currentChanged.connect(self._content_tab_changed)
        self.overview = QTextBrowser()
        self.full_text = QPlainTextEdit()
        self.full_text.setReadOnly(True)
        self.content_tabs.addTab(self.overview, "文件概览")
        self.content_tabs.addTab(self.full_text, "全文")
        self.content_tabs.addTab(self._build_chunk_tab(), "Chunk")
        center_layout.addWidget(self.content_tabs)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.right_tabs = QTabWidget()
        self.right_tabs.currentChanged.connect(self._right_tab_changed)
        self.right_tabs.addTab(self._build_search_tab(), "搜索解释")
        self.right_tabs.addTab(self._build_relations_tab(), "人工关系")
        self.health_text = QPlainTextEdit()
        self.health_text.setReadOnly(True)
        self.right_tabs.addTab(self.health_text, "索引健康")
        right_layout.addWidget(self.right_tabs)

        splitter.addWidget(left)
        splitter.addWidget(center)
        splitter.addWidget(right)
        splitter.setSizes([430, 720, 430])
        return container

    def _build_chunk_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.chunk_list = QListWidget()
        self.chunk_list.setMaximumHeight(180)
        self.chunk_list.currentRowChanged.connect(self.show_chunk)
        layout.addWidget(self.chunk_list)
        self.chunk_text = QPlainTextEdit()
        self.chunk_text.setReadOnly(True)
        layout.addWidget(self.chunk_text)
        buttons = QHBoxLayout()
        previous = QPushButton("上一块")
        previous.clicked.connect(lambda: self.chunk_list.setCurrentRow(max(0, self.chunk_list.currentRow() - 1)))
        following = QPushButton("下一块")
        following.clicked.connect(lambda: self.chunk_list.setCurrentRow(min(self.chunk_list.count() - 1, self.chunk_list.currentRow() + 1)))
        copy = QPushButton("复制 Chunk")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.chunk_text.toPlainText()))
        locate = QPushButton("在全文中定位")
        locate.clicked.connect(self.locate_chunk)
        for button in (previous, following, copy, locate):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        return widget

    def _build_search_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.search_status = QLabel("尚未搜索")
        self.search_status.setWordWrap(True)
        layout.addWidget(self.search_status)
        self.search_results = QTableWidget(0, 8)
        self.search_results.setHorizontalHeaderLabels(["#", "文件", "Chunk", "Vector", "FTS", "RRF", "Rerank", "提升"])
        self.search_results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.search_results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.search_results.verticalHeader().hide()
        self.search_results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.search_results.itemSelectionChanged.connect(self.show_search_result)
        layout.addWidget(self.search_results)
        self.search_detail = QTextBrowser()
        self.search_detail.setMaximumHeight(260)
        layout.addWidget(self.search_detail)
        return widget

    def _build_relations_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.relation_table = QTableWidget(0, 4)
        self.relation_table.setHorizontalHeaderLabels(["类型", "关联文档", "备注", "证据"])
        self.relation_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.relation_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.relation_table.verticalHeader().hide()
        self.relation_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.relation_table)
        buttons = QHBoxLayout()
        add = QPushButton("新建")
        edit = QPushButton("编辑")
        delete = QPushButton("删除")
        add.clicked.connect(self.add_relation)
        edit.clicked.connect(self.edit_relation)
        delete.clicked.connect(self.delete_relation)
        buttons.addWidget(add)
        buttons.addWidget(edit)
        buttons.addWidget(delete)
        buttons.addStretch()
        layout.addLayout(buttons)
        hint = QLabel("关系只写入独立 SQLite，不修改 LanceDB、Embedding 或源文件。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#64748b")
        layout.addWidget(hint)
        return widget

    def _build_graph_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        tools = QHBoxLayout()
        tools.addWidget(QLabel("查看范围："))
        self.graph_scope = QComboBox()
        self.graph_scope.setMinimumWidth(300)
        self.graph_scope.currentIndexChanged.connect(self._graph_scope_changed)
        tools.addWidget(self.graph_scope)
        self.graph_status = QLabel("打开本页后才读取向量并计算语义关系。")
        refresh = QPushButton("刷新图谱")
        refresh.clicked.connect(lambda: self.load_graph(force=True))
        fit = QPushButton("适配全图")
        fit.clicked.connect(lambda: self.graph_view.fit_graph())
        jump = QPushButton("跳转到选中文件")
        jump.clicked.connect(self.jump_from_graph)
        tools.addWidget(self.graph_status)
        tools.addStretch()
        tools.addWidget(refresh)
        tools.addWidget(fit)
        tools.addWidget(jump)
        layout.addLayout(tools)
        legend = QLabel("灰色虚线：语义候选 · 紫色：相关 · 绿色：支持 · 红色：冲突 · 橙色：依赖 · 青色：替代（有向关系带箭头）")
        legend.setStyleSheet("color:#526276")
        layout.addWidget(legend)
        self.graph_scene = QGraphicsScene(self)
        self.graph_scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.graph_view = GraphView(self.graph_scene)
        layout.addWidget(self.graph_view)
        return widget

    def _build_content_graph_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        selectors = QHBoxLayout()
        selectors.addWidget(QLabel("知识库："))
        self.content_graph_kb_combo = QComboBox()
        self.content_graph_kb_combo.setMinimumWidth(220)
        self.content_graph_kb_combo.currentIndexChanged.connect(self._content_graph_kb_changed)
        selectors.addWidget(self.content_graph_kb_combo)
        selectors.addWidget(QLabel("文件："))
        self.content_graph_document_combo = QComboBox()
        self.content_graph_document_combo.setMinimumWidth(260)
        self.content_graph_document_combo.currentIndexChanged.connect(self._content_graph_document_changed)
        selectors.addWidget(self.content_graph_document_combo, 1)
        layout.addLayout(selectors)
        tools = QHBoxLayout()
        self.content_graph_status = QLabel("请在上方选择文件。")
        build = QPushButton("构建/增量更新当前文件")
        build.clicked.connect(self.build_current_content_graph)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.load_content_graph)
        fit = QPushButton("适配当前图")
        fit.clicked.connect(lambda: self.content_graph_view.fit_graph())
        evidence = QPushButton("定位证据Chunk")
        evidence.clicked.connect(self.locate_content_evidence)
        tools.addWidget(self.content_graph_status)
        tools.addStretch()
        tools.addWidget(build)
        tools.addWidget(refresh)
        tools.addWidget(fit)
        tools.addWidget(evidence)
        layout.addLayout(tools)
        legend = QLabel(
            "蓝=文件 · 灰=Chunk · 紫=实体 · 绿=方法 · 橙=观点 · 青=主题 · 粉=数据集；"
            "点击节点或连线查看详情和证据。"
        )
        legend.setStyleSheet("color:#526276")
        layout.addWidget(legend)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_graph_scene = QGraphicsScene(self)
        self.content_graph_scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.content_graph_scene.selectionChanged.connect(self.show_content_graph_selection)
        self.content_graph_view = GraphView(self.content_graph_scene)
        splitter.addWidget(self.content_graph_view)
        self.content_graph_detail = QTextBrowser()
        self.content_graph_detail.setMinimumWidth(260)
        self.content_graph_detail.setHtml(
            "<h3>内容图谱</h3><p>图谱数据独立存储，不修改LanceDB、向量或源文件。</p>"
        )
        splitter.addWidget(self.content_graph_detail)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1300, 300])
        layout.addWidget(splitter, 1)
        return widget

    def _populate_bases(self):
        previous_graph_scope = self.graph_scope.currentData()
        self.kb_combo.blockSignals(True)
        self.kb_combo.clear()
        for base in self.data.bases:
            self.kb_combo.addItem(f"{base['name']} — {base['description']}", base["name"])
        self.kb_combo.blockSignals(False)
        self.content_graph_kb_combo.blockSignals(True)
        self.content_graph_kb_combo.clear()
        for base in self.data.bases:
            self.content_graph_kb_combo.addItem(
                f"{base['name']} — {base['description']}", base["name"]
            )
        content_base_index = self.content_graph_kb_combo.findData(self.current_base())
        self.content_graph_kb_combo.setCurrentIndex(content_base_index if content_base_index >= 0 else 0)
        self.content_graph_kb_combo.blockSignals(False)
        self.graph_scope.blockSignals(True)
        self.graph_scope.clear()
        self.graph_scope.addItem("全部可用知识库", ALL_BASES_SCOPE)
        for base in self.data.bases:
            self.graph_scope.addItem(f"{base['name']} — {base['description']}", base["name"])
        target_scope = previous_graph_scope or self.current_base()
        target_index = self.graph_scope.findData(target_scope)
        self.graph_scope.setCurrentIndex(target_index if target_index >= 0 else 0)
        self.graph_scope.blockSignals(False)

    def current_base(self):
        return self.kb_combo.currentData() or ""

    def refresh_current(self):
        self.data.refresh_bases()
        current = self.current_base()
        self._populate_bases()
        index = self.kb_combo.findData(current)
        if index >= 0:
            self.kb_combo.setCurrentIndex(index)
        self.graph_loaded_for = ""
        self.health_loaded_for = ""
        self.load_documents()

    def load_documents(self):
        name = self.current_base()
        if not name:
            return
        self.current_document = None
        self.current_chunks = []
        self.chunk_generation += 1
        self.document_title.setText("请选择文件")
        self.overview.clear()
        self.full_text.clear()
        self.chunk_list.clear()
        self.chunk_text.clear()
        content_base_index = self.content_graph_kb_combo.findData(name)
        if content_base_index >= 0 and content_base_index != self.content_graph_kb_combo.currentIndex():
            self.content_graph_kb_combo.blockSignals(True)
            self.content_graph_kb_combo.setCurrentIndex(content_base_index)
            self.content_graph_kb_combo.blockSignals(False)
        self._populate_content_graph_documents([])
        if self.main_tabs.currentIndex() == 2:
            self._show_content_graph_empty(
                "正在加载文件列表",
                "文件列表加载完成后，可直接在本页选择文件。",
            )
        self.document_generation += 1
        generation = self.document_generation
        self.document_summary.setText("正在读取文档和原件状态…")
        self._run(lambda: self.data.list_documents(name, resolve_sources=True),
                  lambda result: self._documents_loaded(generation, result))
        if self.right_tabs.currentIndex() == 2:
            self.load_health()

    def _documents_loaded(self, generation, result):
        if generation != self.document_generation or result["knowledge_base"] != self.current_base():
            return
        self.documents = result["documents"]
        self.category_filter.blockSignals(True)
        self.extension_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("全部类别", "")
        for category in sorted(result["categories"]):
            self.category_filter.addItem(category, category if category != "未分类" else "")
        self.extension_filter.clear()
        self.extension_filter.addItem("全部类型", "")
        for extension in sorted(result["extensions"]):
            self.extension_filter.addItem(extension, extension)
        self.category_filter.blockSignals(False)
        self.extension_filter.blockSignals(False)
        self.apply_document_filters()
        self._populate_content_graph_documents(self.documents)
        self._complete_pending_jump()

    def _populate_content_graph_documents(self, documents):
        current_source = self.current_document["source"] if self.current_document else ""
        self.content_graph_document_combo.blockSignals(True)
        self.content_graph_document_combo.clear()
        self.content_graph_document_combo.addItem("请选择一个文件…", "")
        for document in documents:
            self.content_graph_document_combo.addItem(
                f"{document['source']}  ({document['chunk_count']} Chunk)", document["source"]
            )
        index = self.content_graph_document_combo.findData(current_source)
        self.content_graph_document_combo.setCurrentIndex(index if index >= 0 else 0)
        self.content_graph_document_combo.blockSignals(False)

    def _content_graph_kb_changed(self):
        name = self.content_graph_kb_combo.currentData() or ""
        index = self.kb_combo.findData(name)
        if index >= 0 and index != self.kb_combo.currentIndex():
            self.kb_combo.setCurrentIndex(index)

    def _content_graph_document_changed(self):
        source = self.content_graph_document_combo.currentData() or ""
        if not source:
            if not self.current_document:
                self._show_content_graph_empty(
                    "请选择文件",
                    "可直接使用页面上方的知识库和文件下拉框，不必返回文件浏览器。",
                )
            return
        if self.current_document and self.current_document["source"] == source:
            self.load_content_graph()
            return
        self.document_filter.clear()
        self.category_filter.setCurrentIndex(0)
        self.extension_filter.setCurrentIndex(0)
        self._select_visible_source(source)

    def apply_document_filters(self):
        text = self.document_filter.text().casefold()
        category = self.category_filter.currentData() or ""
        extension = self.extension_filter.currentData() or ""
        self.visible_documents = [document for document in self.documents
                                  if (not text or text in document["source"].casefold())
                                  and (not category or document["category"] == category)
                                  and (not extension or document["extension"] == extension)]
        self.document_table.blockSignals(True)
        self.document_table.setRowCount(len(self.visible_documents))
        status_labels = {"resolved": "可打开", "ambiguous": "歧义", "missing": "缺失", "unchecked": "未检查"}
        for row, document in enumerate(self.visible_documents):
            values = [document["source"], str(document["chunk_count"]), document["category"] or "未分类",
                      document["extension"], status_labels.get(document["source_status"], document["source_status"])]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(document["source"])
                self.document_table.setItem(row, column, item)
        self.document_table.blockSignals(False)
        chunks = sum(document["chunk_count"] for document in self.visible_documents)
        self.document_summary.setText(f"显示 {len(self.visible_documents)}/{len(self.documents)} 个文件 · {chunks:,} 个 Chunk")

    def select_document(self):
        row = self.document_table.currentRow()
        if not 0 <= row < len(self.visible_documents):
            return
        document = self.visible_documents[row]
        self.current_document = document
        content_index = self.content_graph_document_combo.findData(document["source"])
        if content_index >= 0 and content_index != self.content_graph_document_combo.currentIndex():
            self.content_graph_document_combo.blockSignals(True)
            self.content_graph_document_combo.setCurrentIndex(content_index)
            self.content_graph_document_combo.blockSignals(False)
        self.document_title.setText(document["source"])
        self.copy_source_button.setEnabled(True)
        self.open_source_button.setEnabled(document["source_status"] in {"resolved", "ambiguous"})
        self.chunk_generation += 1
        generation = self.chunk_generation
        name, source = self.current_base(), document["source"]
        self.overview.setHtml("<p>正在读取 Chunk…</p>")
        self._run_latest_chunk_task(
            lambda: self.data.get_document_chunks(name, source),
            lambda chunks: self._chunks_loaded(generation, document, chunks),
        )
        self.load_relations()
        if self.main_tabs.currentIndex() == 2:
            self.load_content_graph()

    def _chunks_loaded(self, generation, document, chunks):
        if generation != self.chunk_generation or self.current_document is not document:
            return
        self.current_chunks = chunks
        resolution = self.data.resolve_source_file(self.current_base(), document["source"])
        document.update({"source_status": resolution["status"], "source_path": resolution["path"],
                         "source_candidates": resolution["candidates"]})
        self.open_source_button.setEnabled(resolution["status"] in {"resolved", "ambiguous"})
        self.overview.setHtml(
            f"<h3>{document['label']}</h3><p><b>来源：</b>{document['source']}</p>"
            f"<p><b>类别：</b>{document['category'] or '未分类'} · <b>类型：</b>{document['extension']} "
            f"· <b>Chunk：</b>{len(chunks)} · <b>原件：</b>{resolution['status']}</p>"
            "<p>全文与 Chunk 直接来自 LanceDB 的 text/source/chunk_index/category，不读取向量列。</p>"
        )
        self.full_text.setPlainText("切换到“全文”标签后按需生成。")
        self.chunk_list.clear()
        for chunk in chunks:
            self.chunk_list.addItem(f"Chunk #{chunk['chunk_index']} · {len(chunk['text'])} 字 · {chunk['category'] or '未分类'}")
        if chunks:
            self.chunk_list.setCurrentRow(0)
        if self.content_tabs.currentIndex() == 1:
            self._render_full_text()

    def _content_tab_changed(self, index):
        if index == 1:
            self._render_full_text()

    def _render_full_text(self):
        if not self.current_chunks:
            self.full_text.clear()
            return
        expected_marker = f"===== Chunk #{self.current_chunks[0]['chunk_index']} ====="
        if self.full_text.toPlainText().startswith(expected_marker):
            return
        full_parts = [f"===== Chunk #{chunk['chunk_index']} =====\n{chunk['text']}" for chunk in self.current_chunks]
        self.full_text.setPlainText("\n\n".join(full_parts))

    def show_chunk(self, row):
        self.current_chunk_row = row
        if 0 <= row < len(self.current_chunks):
            self.chunk_text.setPlainText(self.current_chunks[row]["text"])

    def locate_chunk(self):
        if not 0 <= self.current_chunk_row < len(self.current_chunks):
            return
        marker = f"===== Chunk #{self.current_chunks[self.current_chunk_row]['chunk_index']} ====="
        self.content_tabs.setCurrentIndex(1)
        self._render_full_text()
        document = self.full_text.document()
        cursor = document.find(marker)
        if not cursor.isNull():
            self.full_text.setTextCursor(cursor)
            self.full_text.centerCursor()

    def copy_source(self):
        if self.current_document:
            QGuiApplication.clipboard().setText(self.current_document.get("source_path") or self.current_document["source"])

    def open_source(self):
        if not self.current_document:
            return
        document = self.current_document
        path = document.get("source_path", "")
        if document["source_status"] == "ambiguous":
            candidates = document.get("source_candidates", [])
            dialog = QDialog(self)
            dialog.setWindowTitle("选择原文件")
            layout = QVBoxLayout(dialog)
            choices = QListWidget()
            choices.addItems(candidates)
            if candidates:
                choices.setCurrentRow(0)
            layout.addWidget(QLabel("发现多个同名文件，请选择："))
            layout.addWidget(choices)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted or choices.currentRow() < 0:
                return
            path = candidates[choices.currentRow()]
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _health_loaded(self, name, health):
        if name != self.current_base():
            return
        self.health_loaded_for = name
        status = health["source_status"]
        duplicate = health["duplicate_groups"] or "无"
        self.health_text.setPlainText(
            f"知识库：{name}\n数据库：{health['database_path']}\n来源根目录：{health['source_roots'] or '未配置'}\n\n"
            f"文档：{health['document_count']}\nChunk：{health['chunk_count']}\n"
            f"原件可解析：{status.get('resolved', 0)}\n路径歧义：{status.get('ambiguous', 0)}\n原件缺失：{status.get('missing', 0)}\n\n"
            f"类别分布：\n{json.dumps(health['categories'], ensure_ascii=False, indent=2)}\n\n"
            f"索引：{health['indices'] or '未报告'}\n疑似重复知识库：{duplicate}"
        )

    def _right_tab_changed(self, index):
        if index == 2:
            self.load_health()

    def load_health(self):
        name = self.current_base()
        if not name or self.health_loaded_for == name:
            return
        self.health_text.setPlainText("正在按需检查索引和来源状态…")
        self._run(lambda: self.data.get_index_health(name),
                  lambda result: self._health_loaded(name, result))

    def run_search(self):
        query = self.search_box.text().strip()
        name = self.current_base()
        if not query or not name:
            return
        self.search_generation += 1
        generation = self.search_generation
        self.search_status.setText("正在搜索…连续发起新搜索时，旧结果会被丢弃。")
        self.right_tabs.setCurrentIndex(0)
        mode = self.search_mode.currentData()
        rerank = self.reranker.isChecked()
        source_filter = self.search_source.text().strip()
        category_filter = self.search_category.text().strip()

        def task():
            import server
            return server.search_knowledge_structured(
                query=query, project=name, search_mode=mode, use_reranker=rerank,
                source_filter=source_filter, category_filter=category_filter, limit=20,
            )

        self._run(task, lambda result: self._search_loaded(generation, result),
                  lambda error: self._search_failed(generation, error))

    def _search_failed(self, generation, error):
        if generation == self.search_generation:
            self.search_status.setText(f"搜索失败：{error.splitlines()[-1]}")

    def _search_loaded(self, generation, payload):
        if generation != self.search_generation:
            return
        self.search_payload = payload
        warning = f" · {payload['warning']}" if payload["warning"] else ""
        self.search_status.setText(
            f"模式：{payload['mode']} · 候选：{payload['candidate_count']} · "
            f"Reranker：{payload['reranker_status']} · 展开查询：{payload['expanded_query']}{warning}"
        )
        results = payload["results"]
        self.search_results.setRowCount(len(results))
        for row, item in enumerate(results):
            values = [item["final_rank"], os.path.basename(item["source"]), item["chunk_index"],
                      item["vector_rank"] or "", item["fts_rank"] or "",
                      f"{item['rrf_score']:.5f}" if item["rrf_score"] is not None else "",
                      f"{item['rerank_score']:.4f}" if item["rerank_score"] is not None else "",
                      "是" if item["category_boost"] else ""]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, row)
                self.search_results.setItem(row, column, cell)
        if results:
            self.search_results.selectRow(0)

    def show_search_result(self):
        row = self.search_results.currentRow()
        payload = getattr(self, "search_payload", None)
        if not payload or not 0 <= row < len(payload["results"]):
            return
        item = payload["results"][row]
        self.search_detail.setHtml(
            f"<b>{item['source']} · Chunk #{item['chunk_index']}</b>"
            f"<p>vector_rank={item['vector_rank']} · distance={item['vector_distance']} · "
            f"fts_rank={item['fts_rank']} · rrf={item['rrf_score']} · category_boost={item['category_boost']} · "
            f"rerank_rank={item['rerank_rank']} · rerank_score={item['rerank_score']} · final_rank={item['final_rank']}</p>"
            f"<pre style='white-space:pre-wrap'>{item['text']}</pre>"
        )
        for index, document in enumerate(self.visible_documents):
            if document["source"] == item["source"]:
                self.document_table.selectRow(index)
                break

    def current_evidence(self):
        if self.current_document and 0 <= self.current_chunk_row < len(self.current_chunks):
            return self.current_document["source"], self.current_chunks[self.current_chunk_row]["chunk_index"]
        return None

    def load_relations(self):
        self.relation_table.setRowCount(0)
        if not self.current_document:
            return
        relations = self.relations.list_for_document(self.current_document["id"])
        self.current_relations = relations
        self.relation_table.setRowCount(len(relations))
        for row, relation in enumerate(relations):
            other = relation["target_id"] if relation["source_id"] == self.current_document["id"] else relation["source_id"]
            evidence = ""
            if relation.get("evidence_chunk_index") is not None:
                evidence = f"{relation['evidence_source']} #{relation['evidence_chunk_index']}"
            values = [RELATION_LABELS.get(relation["relation_type"], relation["relation_type"]), other, relation["note"], evidence]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, relation["id"])
                self.relation_table.setItem(row, column, item)

    def add_relation(self):
        if not self.current_document:
            QMessageBox.information(self, "人工关系", "请先选择一个文档。")
            return
        targets = [document for document in self.documents if document["id"] != self.current_document["id"]]
        dialog = RelationDialog(self, targets, evidence=self.current_evidence())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self.relations.add(self.current_document["id"], **values)
        except (ValueError, sqlite3.IntegrityError) as error:
            QMessageBox.warning(self, "无法建立关系", str(error))
            return
        self.load_relations()
        self.graph_loaded_for = ""

    def selected_relation(self):
        row = self.relation_table.currentRow()
        if 0 <= row < len(getattr(self, "current_relations", [])):
            return self.current_relations[row]
        return None

    def edit_relation(self):
        relation = self.selected_relation()
        if not relation:
            return
        dialog = RelationDialog(self, [], relation=relation, evidence=self.current_evidence())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self.relations.update(relation["id"], values["relation_type"], values["note"],
                                  values["evidence_source"], values["evidence_chunk_index"])
        except sqlite3.IntegrityError as error:
            QMessageBox.warning(self, "无法更新关系", str(error))
            return
        self.load_relations()
        self.graph_loaded_for = ""

    def delete_relation(self):
        relation = self.selected_relation()
        if not relation:
            return
        if QMessageBox.question(self, "删除关系", "确定删除这条人工关系？") != QMessageBox.StandardButton.Yes:
            return
        self.relations.delete(relation["id"])
        self.load_relations()
        self.graph_loaded_for = ""

    def _main_tab_changed(self, index):
        if index == 1:
            self.load_graph()
        elif index == 2:
            if self.current_document:
                self.load_content_graph()
            else:
                self._show_content_graph_empty(
                    "请选择文件",
                    "可直接使用页面上方的知识库和文件下拉框，不必返回文件浏览器。",
                )

    def _graph_scope_changed(self):
        self.graph_loaded_for = ""
        if self.main_tabs.currentIndex() == 1:
            self.load_graph(force=True)

    def load_graph(self, force=False):
        scope = self.graph_scope.currentData()
        if not scope or (not force and self.graph_loaded_for == scope):
            return
        self.graph_generation += 1
        generation = self.graph_generation
        bases = self.data.bases if scope == ALL_BASES_SCOPE else [self.data.base(scope)]
        scope_label = "全部可用知识库" if scope == ALL_BASES_SCOPE else scope
        self.graph_status.setText(f"正在读取 {scope_label} 的向量并计算语义图…")

        def task():
            graph = build_graphs(bases)
            graph["edges"].extend(self.relations.list_for_nodes(node["id"] for node in graph["nodes"]))
            graph["positions"] = force_layout(graph)
            return graph

        self._run(task, lambda graph: self._graph_loaded(generation, scope, graph))

    def _graph_loaded(self, generation, scope, graph):
        if generation != self.graph_generation or scope != self.graph_scope.currentData():
            return
        self.graph = graph
        self.graph_loaded_for = scope
        self.draw_graph()

    def _add_arrow(self, start, end, color):
        line = QLineF(start, end)
        angle = math.atan2(-line.dy(), line.dx())
        size = 10
        p1 = end - QPointF(math.sin(angle + math.pi / 3) * size, math.cos(angle + math.pi / 3) * size)
        p2 = end - QPointF(math.sin(angle + math.pi - math.pi / 3) * size,
                           math.cos(angle + math.pi - math.pi / 3) * size)
        arrow = QGraphicsPolygonItem(QPolygonF([end, p1, p2]))
        arrow.setBrush(color)
        arrow.setPen(QPen(color))
        arrow.setZValue(-1)
        self.graph_scene.addItem(arrow)

    def draw_graph(self):
        self.graph_scene.clear()
        self.node_items = {}
        positions = self.graph.get("positions")
        if positions is None:
            positions = force_layout(self.graph)
        color_by_base = {name: BASE_COLORS[index % len(BASE_COLORS)]
                         for index, name in enumerate(self.graph.get("knowledge_bases", []))}
        for node, position in zip(self.graph["nodes"], positions):
            item = NodeItem(node, position, color_by_base.get(node["knowledge_base"], BASE_COLORS[0]))
            self.graph_scene.addItem(item)
            self.node_items[node["id"]] = item
        for edge in self.graph["edges"]:
            left, right = self.node_items.get(edge["from"]), self.node_items.get(edge["to"])
            if not left or not right:
                continue
            manual = edge.get("kind") == "manual"
            kind = edge.get("relation_type", "related")
            color = QColor(RELATION_COLORS.get(kind, "#94a3b8") if manual else "#94a3b8")
            line = QGraphicsLineItem(QLineF(left.pos(), right.pos()))
            pen = QPen(color, 2.4 if manual else 1.0)
            if not manual:
                pen.setStyle(Qt.PenStyle.DashLine)
            line.setPen(pen)
            line.setZValue(-2)
            line.setToolTip(RELATION_LABELS.get(kind, "语义候选") if manual else f"语义相似度：{edge.get('similarity', '')}")
            self.graph_scene.addItem(line)
            if manual and kind != "related":
                self._add_arrow(left.pos(), right.pos(), color)
        manual_count = sum(edge.get("kind") == "manual" for edge in self.graph["edges"])
        semantic_count = len(self.graph["edges"]) - manual_count
        self.graph_status.setText(f"{len(self.graph['nodes'])} 个文档 · {self.graph['chunks']:,} 个 Chunk · "
                                  f"{semantic_count} 条语义候选 · {manual_count} 条人工关系")
        QTimer.singleShot(0, self.graph_view.fit_graph)

    def jump_from_graph(self):
        selected = [item for item in self.graph_scene.selectedItems() if isinstance(item, NodeItem)]
        if not selected:
            return
        node = selected[0].node
        source = node["source"]
        knowledge_base = node["knowledge_base"]
        self.main_tabs.setCurrentIndex(0)
        self.document_filter.clear()
        if knowledge_base != self.current_base():
            self.pending_source_jump = (knowledge_base, source)
            index = self.kb_combo.findData(knowledge_base)
            if index >= 0:
                self.kb_combo.setCurrentIndex(index)
            return
        self._select_visible_source(source)

    def build_current_content_graph(self):
        if not self.current_document or not self.current_chunks:
            self.content_graph_status.setText("请先选择文件并等待 Chunk 加载完成。")
            return
        self.content_graph_generation += 1
        generation = self.content_graph_generation
        document = dict(self.current_document)
        chunks = [dict(item) for item in self.current_chunks]
        try:
            extractor = extractor_from_environment()
        except Exception as error:
            self.content_graph_status.setText(str(error))
            return
        self.content_graph_status.setText(
            f"正在用 {extractor.name} 抽取 {len(chunks)} 个 Chunk；界面可继续浏览其他内容…"
        )

        def task():
            result = self.content_graph_store.index_document(
                document["knowledge_base"], document["source"], chunks, extractor
            )
            result["extractor"] = extractor.name
            return result

        self._run(
            task,
            lambda result: self._content_graph_indexed(generation, document, result),
            self._content_graph_error,
        )

    def _content_graph_error(self, details):
        self.last_error = details
        append_log(details)
        message = details.splitlines()[-1] if details else "未知错误"
        self.content_graph_status.setText(f"内容图谱构建失败：{message}")

    def _content_graph_indexed(self, generation, document, result):
        if generation != self.content_graph_generation:
            return
        if not self.current_document or self.current_document["id"] != document["id"]:
            return
        status = "无需重复抽取" if result["status"] == "unchanged" else "构建完成"
        self.content_graph_status.setText(
            f"{status} · {result['node_count']} 个节点 · {result['edge_count']} 条证据关系 · {result['extractor']}"
        )
        self.load_content_graph()

    def load_content_graph(self):
        if not self.current_document:
            self.content_graph_status.setText("请在上方选择知识库和文件。")
            self._show_content_graph_empty(
                "尚未选择文件",
                "选择文件后会自动读取已有内容图谱；若尚未建立，可点击“构建/增量更新当前文件”。",
            )
            return
        self.content_graph_generation += 1
        generation = self.content_graph_generation
        document = dict(self.current_document)
        self.content_graph_status.setText(f"正在读取 {document['label']} 的内容图谱…")

        def task():
            graph = self.content_graph_store.get_document_graph(
                document["knowledge_base"], document["source"], limit=300
            )
            graph["positions"] = force_layout(graph)
            return graph

        self._run(
            task,
            lambda graph: self._content_graph_loaded(generation, document, graph),
            self._content_graph_error,
        )

    def _content_graph_loaded(self, generation, document, graph):
        if generation != self.content_graph_generation:
            return
        if not self.current_document or self.current_document["id"] != document["id"]:
            return
        self.content_graph = graph
        self.draw_content_graph()

    def _show_content_graph_empty(self, title, detail):
        self.content_graph = {"nodes": [], "edges": [], "document_id": None}
        self.content_graph_scene.clear()
        self.content_node_items = {}
        self.current_content_evidence = None
        message = self.content_graph_scene.addText(f"{title}\n\n{detail}")
        message.setFont(QFont("Microsoft YaHei", 12))
        message.setDefaultTextColor(QColor("#526276"))
        message.setTextWidth(620)
        message.setPos(36, 36)
        self.content_graph_detail.setPlainText(
            f"{title}\n\n{detail}\n\n内容图谱独立存储，不修改 LanceDB、向量或源文件。"
        )
        QTimer.singleShot(0, self.content_graph_view.fit_graph)

    def draw_content_graph(self):
        self.content_graph_scene.clear()
        self.content_node_items = {}
        self.current_content_evidence = None
        if not self.content_graph.get("document_id"):
            self.content_graph_status.setText("当前文件尚未构建内容图谱。点击“构建/增量更新当前文件”。")
            source = self.current_document["source"] if self.current_document else "当前文件"
            self._show_content_graph_empty(
                "尚无内容图谱",
                f"{source}\n尚未抽取实体、观点和关系。点击右上角“构建/增量更新当前文件”；首次构建默认使用离线抽取。",
            )
            return
        positions = self.content_graph.get("positions")
        if positions is None:
            positions = force_layout(self.content_graph)
        for node, position in zip(self.content_graph["nodes"], positions):
            item = ContentNodeItem(node, position)
            self.content_graph_scene.addItem(item)
            self.content_node_items[node["id"]] = item
        for edge in self.content_graph["edges"]:
            left = self.content_node_items.get(edge["from"])
            right = self.content_node_items.get(edge["to"])
            if not left or not right:
                continue
            self.content_graph_scene.addItem(ContentEdgeItem(edge, QLineF(left.pos(), right.pos())))
        counts = {}
        for node in self.content_graph["nodes"]:
            counts[node["kind"]] = counts.get(node["kind"], 0) + 1
        summary = " · ".join(f"{kind}:{count}" for kind, count in sorted(counts.items()))
        truncated = " · 已按300节点截断" if self.content_graph.get("truncated") else ""
        self.content_graph_status.setText(
            f"{len(self.content_graph['nodes'])} 个节点 · {len(self.content_graph['edges'])} 条关系 · {summary}{truncated}"
        )
        QTimer.singleShot(0, self.content_graph_view.fit_graph)

    def show_content_graph_selection(self):
        selected = self.content_graph_scene.selectedItems()
        self.current_content_evidence = None
        edge_item = next((item for item in selected if isinstance(item, ContentEdgeItem)), None)
        if edge_item:
            edge = edge_item.edge
            chunk_index = edge.get("evidence_chunk_index")
            if chunk_index is not None and self.current_document:
                self.current_content_evidence = {
                    "knowledge_base": self.current_document["knowledge_base"],
                    "source": self.current_document["source"],
                    "chunk_index": int(chunk_index),
                }
            self.content_graph_detail.setPlainText(
                f"关系：{edge.get('relation_type', '')}\n"
                f"置信度：{float(edge.get('confidence', 0)):.2f}\n"
                f"来源：{edge.get('origin', '')}\n"
                f"证据：Chunk #{chunk_index}\n"
                f"备注：{edge.get('note', '')}\n\n"
                f"{edge.get('evidence_preview', '')}"
            )
            return
        node_item = next((item for item in selected if isinstance(item, ContentNodeItem)), None)
        if not node_item:
            return
        node = node_item.node
        properties = node.get("properties") or {}
        chunk_index = properties.get("chunk_index")
        if chunk_index is not None and self.current_document:
            self.current_content_evidence = {
                "knowledge_base": self.current_document["knowledge_base"],
                "source": self.current_document["source"],
                "chunk_index": int(chunk_index),
            }
        self.content_graph_detail.setPlainText(
            f"{node.get('label', '')}\n\n类型：{node.get('kind', '')}\n"
            f"说明：{node.get('description', '')}\n"
            f"属性：{json.dumps(properties, ensure_ascii=False, indent=2)}"
        )

    def locate_content_evidence(self):
        evidence = self.current_content_evidence
        if not evidence or not self.current_document:
            self.content_graph_status.setText("请先选择一条带证据的关系或Chunk节点。")
            return
        target_row = next(
            (row for row, chunk in enumerate(self.current_chunks)
             if chunk["chunk_index"] == evidence["chunk_index"]),
            -1,
        )
        if target_row < 0:
            self.content_graph_status.setText("证据Chunk不在当前加载的文档中。")
            return
        self.main_tabs.setCurrentIndex(0)
        self.content_tabs.setCurrentIndex(2)
        self.chunk_list.setCurrentRow(target_row)
        self.chunk_list.scrollToItem(self.chunk_list.item(target_row))

    def _select_visible_source(self, source):
        self.apply_document_filters()
        for row, document in enumerate(self.visible_documents):
            if document["source"] == source:
                self.document_table.selectRow(row)
                if not self.current_document or self.current_document["source"] != source:
                    self.select_document()
                self.document_table.scrollToItem(self.document_table.item(row, 0))
                break

    def _complete_pending_jump(self):
        if not self.pending_source_jump:
            return
        knowledge_base, source = self.pending_source_jump
        if knowledge_base != self.current_base():
            return
        self.pending_source_jump = None
        self._select_visible_source(source)


# Compatibility for code that imported the former window class.
KnowledgeGraphWindow = KnowledgeBrowserWindow


def smoke_test(config_path: str, relations_path: str, content_graph_path: str) -> int:
    data = KnowledgeBrowserData(config_path)
    if not data.bases:
        raise RuntimeError("没有可用知识库。")
    names = [base["name"] for base in data.bases]
    name = "project-通用" if "project-通用" in names else names[0]
    listing = data.list_documents(name, limit=1)
    if not listing["documents"] or listing["total_chunks"] <= 0:
        raise RuntimeError(f"知识库 {name} 没有文档。")
    source = listing["documents"][0]["source"]
    chunks = data.get_document_chunks(name, source)
    if not chunks or [item["chunk_index"] for item in chunks] != sorted(item["chunk_index"] for item in chunks):
        raise RuntimeError("Chunk 读取或排序失败。")
    relations = RelationStore(relations_path)
    relations.remap_knowledge_bases(data.aliases)
    content_graph = ContentGraphStore(content_graph_path)
    content_graph.remap_knowledge_bases(data.aliases)
    graph_stats = content_graph.stats()
    import server
    if not callable(getattr(server, "search_knowledge_structured", None)):
        raise RuntimeError("结构化搜索入口不可用。")
    print(json.dumps({"knowledge_bases": names, "selected": name, "documents": listing["total_documents"],
                      "chunks": listing["total_chunks"], "checked_source": source,
                      "checked_source_chunks": len(chunks), "content_graph": graph_stats}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description="LanceDB 知识浏览器")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--relations", default=DEFAULT_RELATIONS)
    parser.add_argument("--content-graph", default=DEFAULT_CONTENT_GRAPH)
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    install_crash_logging()
    if args.smoke_test:
        try:
            return smoke_test(args.config, args.relations, args.content_graph)
        except Exception:
            details = traceback.format_exc()
            try:
                with open(args.relations + ".smoke-error.log", "w", encoding="utf-8") as stream:
                    stream.write(details)
            except OSError:
                pass
            print(details, file=sys.stderr)
            return 1
    app = QApplication(sys.argv)
    app.setApplicationName("LanceDB 知识浏览器")
    window = KnowledgeBrowserWindow(args.config, args.relations, args.content_graph)
    window.show()
    return app.exec()


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        plugin_path = os.path.join(RESOURCE_DIR, "PySide6", "plugins")
        if os.path.isdir(plugin_path):
            QCoreApplication.addLibraryPath(plugin_path)
    raise SystemExit(main())
