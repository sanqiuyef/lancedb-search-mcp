# -*- coding: utf-8 -*-
"""kb —— LanceDB 知识库核心包。

模块一览（自上而下按调用层次）：
  config      环境变量、库路径解析（单库扁平，无分区）
  embeddings  文本向量化（本地 sentence-transformers，闲置自动卸载）
  schema      LanceModel 表结构、建表、FTS/向量索引维护
  ingest      文档解析（OCR）、分块、增删改查
  search      官方 hybrid 检索 + 本地 CrossEncoder 精排
  model_lifecycle  本地模型用时挂载/闲置卸载
  ask         RAG 证据检索（答问由调用方模型完成）
  web         [自建模块 · 待优化] 网页抓取入库
  watcher     [自建模块 · 待优化] watchdog 目录监听
"""
