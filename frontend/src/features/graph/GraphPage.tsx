import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, InputNumber, List, Select, Space, Tag, Typography } from "antd";
import { DeploymentUnitOutlined } from "@ant-design/icons";
import cytoscape from "cytoscape";
import { api } from "../../shared/api/client";
import type { GraphEdge } from "../../shared/types/api";

interface Props {
  novelId: string | null;
  initialCenterId: string | null;
  onOpenEvidence: (evidenceId: string) => void;
}

export function GraphPage({ novelId, initialCenterId, onOpenEvidence }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [centerId, setCenterId] = useState<string | null>(initialCenterId);
  const [depth, setDepth] = useState<number>(1);
  const [relationTypes, setRelationTypes] = useState<string | undefined>(undefined);
  const [chapterFrom, setChapterFrom] = useState<number | undefined>(undefined);
  const [chapterTo, setChapterTo] = useState<number | undefined>(undefined);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null);

  useEffect(() => {
    if (initialCenterId) setCenterId(initialCenterId);
  }, [initialCenterId]);

  const charsQuery = useQuery({
    queryKey: ["graph-char-options", novelId],
    queryFn: () => api.listCharacters(novelId!, { page: 1, pageSize: 50 }),
    enabled: !!novelId,
  });

  useEffect(() => {
    if (!centerId && charsQuery.data?.items?.length) {
      setCenterId(charsQuery.data.items[0].id);
    }
  }, [centerId, charsQuery.data]);

  const graphQuery = useQuery({
    queryKey: ["ego-graph", centerId, depth, relationTypes, chapterFrom, chapterTo],
    queryFn: () =>
      api.getGraphNeighbors({
        center_character_id: centerId!,
        depth,
        relation_types: relationTypes,
        chapter_from: chapterFrom,
        chapter_to: chapterTo,
      }),
    enabled: !!centerId,
  });

  const edgeEvidenceQuery = useQuery({
    queryKey: ["rel-evidence", selectedEdge?.id],
    queryFn: () => api.getRelationshipEvidence(selectedEdge!.id),
    enabled: !!selectedEdge,
  });

  useEffect(() => {
    if (!containerRef.current || !graphQuery.data) return;

    const elements: cytoscape.ElementDefinition[] = [
      ...graphQuery.data.nodes.map((n) => ({
        data: {
          id: n.id,
          label: n.label,
          isCenter: n.is_center ? "yes" : "no",
        },
      })),
      ...graphQuery.data.edges.map((e) => ({
        data: {
          id: e.id,
          source: e.source,
          target: e.target,
          label: e.relation_type,
          raw: e,
        },
      })),
    ];

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#1677ff",
            label: "data(label)",
            color: "#1f1f1f",
            "font-size": 12,
            "text-valign": "bottom",
            "text-margin-y": 6,
            width: 34,
            height: 34,
          },
        },
        {
          selector: 'node[isCenter = "yes"]',
          style: {
            "background-color": "#fa541c",
            width: 46,
            height: 46,
            "font-weight": "bold",
          },
        },
        {
          selector: "edge",
          style: {
            width: 2,
            "line-color": "#91caff",
            "target-arrow-color": "#91caff",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            label: "data(label)",
            "font-size": 10,
            color: "#595959",
            "text-rotation": "autorotate",
          },
        },
        {
          selector: "edge:selected",
          style: {
            width: 4,
            "line-color": "#fa541c",
            "target-arrow-color": "#fa541c",
          },
        },
      ],
      layout: { name: "cose", animate: false, padding: 30 },
    });

    cy.on("tap", "edge", (evt) => {
      const raw = evt.target.data("raw") as GraphEdge;
      setSelectedEdge(raw);
    });

    cy.on("tap", "node", (evt) => {
      const nid = evt.target.id();
      if (nid && nid !== centerId) {
        setCenterId(nid);
      }
    });

    return () => {
      cy.destroy();
    };
  }, [graphQuery.data, centerId]);

  if (!novelId) {
    return <Card>请先在「小说项目」页面导入并选择一部小说。</Card>;
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card
        title={
          <Space>
            <DeploymentUnitOutlined />
            <span>局部人物关系图谱 (Cytoscape.js)</span>
          </Space>
        }
      >
        <Space wrap style={{ marginBottom: 16 }}>
          <Select
            showSearch
            optionFilterProp="label"
            placeholder="选择中心人物"
            style={{ width: 200 }}
            value={centerId || undefined}
            onChange={(v) => {
              setCenterId(v);
              setSelectedEdge(null);
            }}
            options={(charsQuery.data?.items || []).map((c) => ({
              value: c.id,
              label: `${c.canonical_name} (${c.mention_count})`,
            }))}
          />
          <Select
            value={depth}
            style={{ width: 130 }}
            onChange={setDepth}
            options={[
              { value: 1, label: "1 跳直接邻居" },
              { value: 2, label: "2 跳扩展网络" },
            ]}
          />
          <Select
            allowClear
            placeholder="筛选关系类型"
            style={{ width: 150 }}
            onChange={setRelationTypes}
            options={[
              { value: "master", label: "师父" },
              { value: "disciple", label: "徒弟" },
              { value: "ally", label: "盟友" },
              { value: "enemy", label: "敌对" },
              { value: "rival", label: "竞争" },
              { value: "acquaintance", label: "相识" },
              { value: "avatar_of", label: "分身/化身" },
            ]}
          />
          <InputNumber
            min={1}
            placeholder="起始章"
            value={chapterFrom}
            onChange={(v) => setChapterFrom(v || undefined)}
          />
          <InputNumber
            min={1}
            placeholder="结束章"
            value={chapterTo}
            onChange={(v) => setChapterTo(v || undefined)}
          />
        </Space>

        {graphQuery.data?.truncated && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="节点或关系数已达到局部图谱上限，请使用关系类型或章节区间进一步筛选。"
          />
        )}

        <div
          ref={containerRef}
          style={{
            width: "100%",
            height: 480,
            border: "1px solid #f0f0f0",
            borderRadius: 8,
            background: "#fcfcfd",
          }}
        />
        <Typography.Text type="secondary" style={{ display: "block", marginTop: 8 }}>
          提示：点击任意人物节点可将其切换为中心人物；点击关系连线可查看对应原文证据。
        </Typography.Text>
      </Card>

      {selectedEdge && (
        <Card
          size="small"
          title={
            <Space>
              <span>关系证据详情</span>
              <Tag color="volcano">{selectedEdge.relation_type}</Tag>
              <Tag>{selectedEdge.review_status}</Tag>
            </Space>
          }
        >
          {selectedEdge.description && <p>{selectedEdge.description}</p>}
          <List
            size="small"
            loading={edgeEvidenceQuery.isLoading}
            dataSource={edgeEvidenceQuery.data || []}
            renderItem={(ev) => (
              <List.Item
                actions={[
                  <Button key="view" size="small" type="link" onClick={() => onOpenEvidence(ev.id)}>
                    定位原文
                  </Button>,
                ]}
              >
                <List.Item.Meta
                  title={`「${ev.quote_text}」`}
                  description={`校验状态: ${ev.verification_status} · 偏移 [${ev.start_offset}, ${ev.end_offset})`}
                />
              </List.Item>
            )}
          />
        </Card>
      )}
    </Space>
  );
}
