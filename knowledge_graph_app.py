# -*- coding: utf-8 -*-
"""Local, user-operated knowledge graph viewer for LanceDB knowledge bases."""

import argparse
import json
import os
import sqlite3
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from knowledge_graph import available_knowledge_bases, build_graphs, load_knowledge_bases


SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SERVER_DIR, "kb-config.json")
DEFAULT_RELATIONS = os.path.join(SERVER_DIR, "knowledge_graph_relations.sqlite3")


class RelationStore:
    """Persist user-confirmed links separately from source documents and LanceDB."""

    def __init__(self, path: str):
        self.path = path
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS relations (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    PRIMARY KEY (source_id, target_id),
                    CHECK (source_id < target_id)
                )"""
            )

    def _connect(self):
        return sqlite3.connect(self.path)

    def list_for_nodes(self, node_ids):
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        sql = f"SELECT source_id, target_id FROM relations WHERE source_id IN ({placeholders}) AND target_id IN ({placeholders})"
        with self._connect() as connection:
            return [
                {"from": source, "to": target, "kind": "manual", "value": 3}
                for source, target in connection.execute(sql, [*node_ids, *node_ids])
            ]

    def add(self, source_id: str, target_id: str):
        if not source_id or not target_id or source_id == target_id:
            raise ValueError("请选择两个不同的文档节点。")
        source_id, target_id = sorted((source_id, target_id))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO relations (source_id, target_id) VALUES (?, ?)",
                (source_id, target_id),
            )


def viewer_html() -> str:
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>知识库图谱</title><script src="/assets/vis-network.min.js"></script>
<style>
:root{color:#172033;background:#f4f7fb;font:14px system-ui,"Microsoft YaHei",sans-serif}*{box-sizing:border-box}body{margin:0}header{height:76px;padding:15px 24px;background:#fff;border-bottom:1px solid #dce3ee;display:flex;align-items:center;gap:14px}h1{font-size:20px;margin:0 18px 0 0}select,button{font:inherit;border:1px solid #bcc9da;border-radius:7px;padding:8px 10px;background:#fff}button{cursor:pointer;background:#2563eb;color:#fff;border-color:#2563eb}button:hover{background:#1d4ed8}#summary{color:#56657b;margin-left:auto}.layout{height:calc(100vh - 76px);min-height:560px;display:grid;grid-template-columns:1fr 360px}.network{min-width:0}.panel{background:#fff;border-left:1px solid #dce3ee;padding:18px;overflow:auto}.panel h2{font-size:16px;margin:0 0 12px}.field{margin:12px 0}.field label{display:block;color:#56657b;margin-bottom:5px}.field select{width:100%}.muted{color:#64748b;font-size:12px;line-height:1.55;word-break:break-all}.hidden{display:none}.legend{margin-top:18px;border-top:1px solid #e7edf5;padding-top:13px}.line{display:inline-block;width:28px;border-top:2px solid #94a3b8;margin-right:7px;vertical-align:middle}.line.manual{border-color:#7c3aed}.line.semantic{border-top-style:dashed}@media(max-width:800px){header{height:auto;flex-wrap:wrap}#summary{margin-left:0}.layout{height:auto;grid-template-columns:1fr}.network{height:65vh}.panel{border-left:0;border-top:1px solid #dce3ee}}
</style></head><body>
<header><h1>知识库图谱</h1><label>查看范围 <select id="scope"></select></label><button id="refresh">刷新图谱</button><span id="summary">正在加载…</span></header>
<main class="layout"><section id="network" class="network" aria-label="知识库关联图"></section><aside class="panel"><h2 id="nodeTitle">选择一个文档</h2><div id="nodeInfo" class="muted">节点大小表示文本块数量。虚线表示自动识别的语义关联；紫色实线表示用户创建的 Wiki 式链接。</div><div id="linkEditor" class="hidden"><div class="field"><label for="target">链接到</label><select id="target"></select></div><button id="link">建立链接</button><p class="muted">链接会保存在本地图谱数据库中，不会修改原始文档。</p></div><div class="legend"><div><span class="line semantic"></span>自动语义关联</div><div><span class="line manual"></span>用户链接</div></div></aside></main>
<script>
let network, nodes, selectedNode, currentGraph;
const colors=['#2563eb','#16a34a','#d97706','#9333ea','#db2777','#0891b2'];
const esc=value=>String(value ?? '');
async function request(path, options){const response=await fetch(path,options);const body=await response.json();if(!response.ok)throw new Error(body.error||'请求失败');return body}
function showNode(id){selectedNode=nodes.get(id);if(!selectedNode)return;document.getElementById('nodeTitle').textContent=selectedNode.label;const info=document.getElementById('nodeInfo');info.replaceChildren();for(const line of [`知识库：${selectedNode.knowledge_base}`,`文本块：${selectedNode.chunks} · 类别：${selectedNode.category} · 类型：.${selectedNode.group}`,`来源：${selectedNode.source}`]){const div=document.createElement('div');div.textContent=line;info.append(div)}const target=document.getElementById('target');target.replaceChildren();for(const node of currentGraph.nodes.filter(node=>node.id!==id).sort((a,b)=>(a.knowledge_base+a.label).localeCompare(b.knowledge_base+b.label,'zh-CN'))){const option=document.createElement('option');option.value=node.id;option.textContent=`[${node.knowledge_base}] ${node.label}`;target.append(option)}document.getElementById('linkEditor').classList.remove('hidden')}
function render(graph){currentGraph=graph;const bases=[...new Set(graph.nodes.map(node=>node.knowledge_base))];const palette=new Map(bases.map((base,index)=>[base,colors[index%colors.length]]));nodes=new vis.DataSet(graph.nodes.map(node=>({...node,color:{background:palette.get(node.knowledge_base),border:'#ffffff'},font:{color:'#172033'}})));const edges=new vis.DataSet(graph.edges.map(edge=>edge.kind==='manual'?({...edge,color:{color:'#7c3aed',highlight:'#5b21b6'},width:2,title:'用户链接'}):({...edge,color:{color:'#94a3b8',highlight:'#2563eb'},dashes:true,width:1,title:`语义相似度：${edge.similarity}`})));if(network)network.destroy();network=new vis.Network(document.getElementById('network'),{nodes,edges},{autoResize:true,nodes:{shape:'dot',borderWidth:2,font:{face:'Microsoft YaHei',size:13}},edges:{smooth:{type:'continuous'}},interaction:{hover:true,navigationButtons:true,keyboard:true},physics:{stabilization:{iterations:180},barnesHut:{gravitationalConstant:-6200,springLength:155}}});network.on('click',params=>{if(params.nodes.length)showNode(params.nodes[0])});document.getElementById('summary').textContent=`知识库 ${graph.knowledge_bases.length} 个 · 文档 ${graph.nodes.length} 篇 · 文本块 ${graph.chunks.toLocaleString()} 个 · 关联 ${graph.edges.length} 条`;}
async function loadGraph(){const scope=document.getElementById('scope').value;document.getElementById('summary').textContent='正在生成图谱…';document.getElementById('linkEditor').classList.add('hidden');selectedNode=null;try{render(await request(`/api/graph?scope=${encodeURIComponent(scope)}`))}catch(error){document.getElementById('summary').textContent=error.message}}
async function loadBases(){const data=await request('/api/knowledge-bases');const scope=document.getElementById('scope');scope.replaceChildren();for(const base of data.knowledge_bases){const option=document.createElement('option');option.value=base.name;option.textContent=base.label;scope.append(option)}scope.value=data.default_scope;await loadGraph()}
document.getElementById('refresh').addEventListener('click',loadGraph);document.getElementById('scope').addEventListener('change',loadGraph);document.getElementById('link').addEventListener('click',async()=>{if(!selectedNode)return;const selectedId=selectedNode.id;try{await request('/api/relations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:selectedId,target_id:document.getElementById('target').value})});await loadGraph();showNode(selectedId)}catch(error){alert(error.message)}});loadBases().catch(error=>document.getElementById('summary').textContent=error.message);
</script></body></html>"""


class GraphViewerHandler(BaseHTTPRequestHandler):
    config_path = DEFAULT_CONFIG
    relation_store = None
    asset_path = os.path.join(SERVER_DIR, "web_assets", "vis-network.min.js")

    def log_message(self, _format, *_args):
        return

    def _send_json(self, payload, status=HTTPStatus.OK):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, page):
        encoded = page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _bases(self):
        return available_knowledge_bases(load_knowledge_bases(self.config_path))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(viewer_html())
            return
        if parsed.path == "/assets/vis-network.min.js":
            try:
                with open(self.asset_path, "rb") as asset:
                    content = asset.read()
            except FileNotFoundError:
                self._send_json({"error": "缺少本地图谱组件，请重新安装启动程序。"}, HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path == "/api/knowledge-bases":
            bases = self._bases()
            self._send_json({
                "knowledge_bases": [{"name": "all", "label": "全部可用知识库"}] + [
                    {"name": base["name"], "label": f"{base['name']} — {base['description']}"}
                    for base in bases
                ],
                "default_scope": "project-通用" if any(base["name"] == "project-通用" for base in bases) else "all",
            })
            return
        if parsed.path == "/api/graph":
            scope = parse_qs(parsed.query).get("scope", ["all"])[0]
            bases = self._bases()
            selected = bases if scope == "all" else [base for base in bases if base["name"] == scope]
            if not selected:
                self._send_json({"error": "所选知识库不可用或为空。"}, HTTPStatus.NOT_FOUND)
                return
            graph = build_graphs(selected)
            node_ids = [node["id"] for node in graph["nodes"]]
            graph["edges"].extend(self.relation_store.list_for_nodes(node_ids))
            self._send_json(graph)
            return
        self._send_json({"error": "未找到该页面。"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if urlparse(self.path).path != "/api/relations":
            self._send_json({"error": "未找到该接口。"}, HTTPStatus.NOT_FOUND)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            self.relation_store.add(payload.get("source_id", ""), payload.get("target_id", ""))
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True}, HTTPStatus.CREATED)


def main():
    parser = argparse.ArgumentParser(description="启动本地知识库图谱浏览器")
    parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765）")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="知识库配置文件")
    parser.add_argument("--relations", default=DEFAULT_RELATIONS, help="用户链接的 SQLite 文件")
    parser.add_argument("--no-browser", action="store_true", help="仅启动服务，不自动打开浏览器")
    args = parser.parse_args()

    GraphViewerHandler.config_path = os.path.abspath(args.config)
    GraphViewerHandler.relation_store = RelationStore(os.path.abspath(args.relations))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), GraphViewerHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"知识库图谱已启动：{url}")
    print("关闭此窗口即可停止服务。")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
