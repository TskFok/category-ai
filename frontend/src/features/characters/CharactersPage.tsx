import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Input,
  List,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { DeploymentUnitOutlined, FileTextOutlined, UserOutlined } from "@ant-design/icons";
import { api } from "../../shared/api/client";
import type { CharacterSummary } from "../../shared/types/api";

interface Props {
  novelId: string | null;
  onOpenEvidence: (evidenceId: string) => void;
  onOpenGraphForCharacter: (characterId: string) => void;
}

const STATUS_COLORS: Record<string, string> = {
  confirmed: "green",
  candidate: "blue",
  probable: "cyan",
  unresolved: "orange",
  rejected: "red",
  superseded: "default",
};

export function CharactersPage({ novelId, onOpenEvidence, onOpenGraphForCharacter }: Props) {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [page, setPage] = useState(1);
  const [selectedCharId, setSelectedCharId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ["characters", novelId, search, statusFilter, page],
    queryFn: () =>
      api.listCharacters(novelId!, {
        q: search || undefined,
        status: statusFilter,
        page,
        pageSize: 20,
      }),
    enabled: !!novelId,
  });

  const detailQuery = useQuery({
    queryKey: ["character-detail", selectedCharId],
    queryFn: () => api.getCharacter(selectedCharId!),
    enabled: !!selectedCharId,
  });

  const mentionsQuery = useQuery({
    queryKey: ["character-mentions", selectedCharId],
    queryFn: () => api.getCharacterMentions(selectedCharId!, 1),
    enabled: !!selectedCharId,
  });

  const eventsQuery = useQuery({
    queryKey: ["character-events", selectedCharId],
    queryFn: () => api.getCharacterEvents(selectedCharId!),
    enabled: !!selectedCharId,
  });

  const relsQuery = useQuery({
    queryKey: ["character-rels", selectedCharId],
    queryFn: () => api.getCharacterRelationships(selectedCharId!),
    enabled: !!selectedCharId,
  });

  if (!novelId) {
    return <Card>请先在「小说项目」页面导入并选择一部小说。</Card>;
  }

  return (
    <>
      <Card
        title={
          <Space>
            <UserOutlined />
            <span>人物检索与档案</span>
          </Space>
        }
        extra={
          <Space>
            <Input.Search
              placeholder="按姓名或别名搜索"
              allowClear
              style={{ width: 240 }}
              onSearch={(v) => {
                setSearch(v);
                setPage(1);
              }}
            />
            <Select
              allowClear
              placeholder="审核状态"
              style={{ width: 140 }}
              onChange={(v) => {
                setStatusFilter(v);
                setPage(1);
              }}
              options={[
                { value: "confirmed", label: "confirmed" },
                { value: "candidate", label: "candidate" },
                { value: "probable", label: "probable" },
                { value: "unresolved", label: "unresolved" },
              ]}
            />
          </Space>
        }
      >
        <Table
          rowKey="id"
          loading={listQuery.isLoading}
          dataSource={listQuery.data?.items || []}
          pagination={{
            current: page,
            pageSize: 20,
            total: listQuery.data?.total || 0,
            onChange: setPage,
          }}
          columns={[
            {
              title: "标准名",
              dataIndex: "canonical_name",
              render: (text: string, rec: CharacterSummary) => (
                <Typography.Link strong onClick={() => setSelectedCharId(rec.id)}>
                  {text}
                </Typography.Link>
              ),
            },
            { title: "类型", dataIndex: "entity_type", width: 110 },
            {
              title: "状态",
              dataIndex: "status",
              width: 120,
              render: (s: string) => <Tag color={STATUS_COLORS[s] || "default"}>{s}</Tag>,
            },
            {
              title: "首次出场",
              dataIndex: "first_appearance_chapter",
              width: 110,
              render: (v: number | null) => (v ? `第 ${v} 章` : "-"),
            },
            { title: "提及次数", dataIndex: "mention_count", width: 100 },
            {
              title: "操作",
              width: 200,
              render: (_, rec: CharacterSummary) => (
                <Space>
                  <Button size="small" onClick={() => setSelectedCharId(rec.id)}>
                    档案
                  </Button>
                  <Button
                    size="small"
                    icon={<DeploymentUnitOutlined />}
                    onClick={() => onOpenGraphForCharacter(rec.id)}
                  >
                    关系图
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Drawer
        title={detailQuery.data ? `人物档案：${detailQuery.data.canonical_name}` : "人物档案"}
        width={720}
        open={!!selectedCharId}
        onClose={() => setSelectedCharId(null)}
        extra={
          selectedCharId && (
            <Button
              type="primary"
              icon={<DeploymentUnitOutlined />}
              onClick={() => {
                onOpenGraphForCharacter(selectedCharId);
                setSelectedCharId(null);
              }}
            >
              在关系图中打开
            </Button>
          )
        }
      >
        {detailQuery.data && (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="实体类型">{detailQuery.data.entity_type}</Descriptions.Item>
              <Descriptions.Item label="审核状态">
                <Tag color={STATUS_COLORS[detailQuery.data.status]}>
                  {detailQuery.data.status}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="首次出场">
                {detailQuery.data.first_appearance_chapter
                  ? `第 ${detailQuery.data.first_appearance_chapter} 章`
                  : "-"}
              </Descriptions.Item>
              <Descriptions.Item label="提及总数">
                {detailQuery.data.mention_count}
              </Descriptions.Item>
              <Descriptions.Item label="已关联别名" span={2}>
                {detailQuery.data.aliases.length > 0
                  ? detailQuery.data.aliases.map((a) => (
                      <Tag key={a.id}>
                        {a.alias_text} ({a.review_status})
                      </Tag>
                    ))
                  : "暂无"}
              </Descriptions.Item>
            </Descriptions>

            <Tabs
              items={[
                {
                  key: "facts",
                  label: `原子事实 (${detailQuery.data.facts.length})`,
                  children: (
                    <List
                      size="small"
                      dataSource={detailQuery.data.facts}
                      renderItem={(f) => (
                        <List.Item
                          actions={f.evidence_ids.map((eid) => (
                            <Button
                              key={eid}
                              size="small"
                              type="link"
                              icon={<FileTextOutlined />}
                              onClick={() => onOpenEvidence(eid)}
                            >
                              查看证据
                            </Button>
                          ))}
                        >
                          <List.Item.Meta
                            title={
                              <Space>
                                <Tag>{f.predicate}</Tag>
                                <span>{JSON.stringify(f.value_json.value ?? f.value_json)}</span>
                                {f.chapter_no && <Tag color="geekblue">第{f.chapter_no}章</Tag>}
                              </Space>
                            }
                            description={`确定性: ${f.certainty} · 状态: ${f.review_status}`}
                          />
                        </List.Item>
                      )}
                    />
                  ),
                },
                {
                  key: "states",
                  label: `修为/势力演化 (${detailQuery.data.states.length})`,
                  children: (
                    <Timeline
                      items={detailQuery.data.states.map((s) => ({
                        children: (
                          <span>
                            <Tag color="purple">{s.state_type}</Tag>
                            <strong>{JSON.stringify(s.value_json.value ?? s.value_json)}</strong>
                            {s.valid_from && ` (第 ${s.valid_from} 章起)`}
                          </span>
                        ),
                      }))}
                    />
                  ),
                },
                {
                  key: "events",
                  label: `事件时间线 (${eventsQuery.data?.length || 0})`,
                  children: (
                    <Timeline
                      items={(eventsQuery.data || []).map((ev) => ({
                        children: (
                          <Space direction="vertical" size={2}>
                            <Space>
                              {ev.chapter_no && <Tag>第{ev.chapter_no}章</Tag>}
                              <strong>{ev.title}</strong>
                              <Tag color="blue">{ev.participant_role}</Tag>
                            </Space>
                            {ev.evidence_ids.length > 0 && (
                              <Button
                                size="small"
                                type="link"
                                onClick={() => onOpenEvidence(ev.evidence_ids[0])}
                              >
                                查看原文证据
                              </Button>
                            )}
                          </Space>
                        ),
                      }))}
                    />
                  ),
                },
                {
                  key: "rels",
                  label: `直接关系 (${relsQuery.data?.length || 0})`,
                  children: (
                    <List
                      size="small"
                      dataSource={relsQuery.data || []}
                      renderItem={(r) => (
                        <List.Item
                          actions={r.evidence_ids.map((eid) => (
                            <Button
                              key={eid}
                              size="small"
                              type="link"
                              onClick={() => onOpenEvidence(eid)}
                            >
                              证据
                            </Button>
                          ))}
                        >
                          <Space>
                            <strong>{r.source_character_name}</strong>
                            <Tag color="volcano">{r.relation_type}</Tag>
                            <strong>{r.target_character_name}</strong>
                            <span style={{ color: "#888" }}>{r.description}</span>
                          </Space>
                        </List.Item>
                      )}
                    />
                  ),
                },
                {
                  key: "mentions",
                  label: `出场提及 (${mentionsQuery.data?.total || 0})`,
                  children: (
                    <List
                      size="small"
                      dataSource={mentionsQuery.data?.items || []}
                      renderItem={(m) => (
                        <List.Item>
                          <List.Item.Meta
                            title={`第 ${m.chapter_no} 章 ${m.chapter_title} · 「${m.surface_text}」`}
                            description={m.context_snippet}
                          />
                        </List.Item>
                      )}
                    />
                  ),
                },
              ]}
            />
          </Space>
        )}
      </Drawer>
    </>
  );
}
