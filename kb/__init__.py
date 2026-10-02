# -*- coding: utf-8 -*-
"""kb —— LanceDB 知识库核心包。

模块一览（自上而下按调用层次）：
  config      环境变量、库路径解析、kb-config.json 分区注册表
  embeddings  文本向量化（官方注册表：siliconflow / sentence-transformers）
  schema      LanceModel 表结构、建表、FTS/向量索引维护
  ingest      文档解析（OCR）、分块、增删改查
  search      官方 hybrid 检索 + Reranker
  ask         RAG 问答
  web         [自建模块 · 待优化] 网页抓取入库
  watcher     [自建模块 · 待优化] watchdog 目录监听
"""
