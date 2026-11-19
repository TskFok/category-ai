import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Descriptions, Empty, List, Select, Space, Tag, Typography } from "antd";
import { FileSearchOutlined } from "@ant-design/icons";
import { api } from "../../shared/api/client";

interface Props {
  novelId: string | null;
  onOpenEvidence: (evidenceId: string) => void;
}

const CLAIM_TAG: Record<string, { color: string; label: string }> = {
  fact: { color: "green", label: "原文事实" },
  inference: { color: "purple", label: "模型推断" },
  unknown: { color: "default", label: "未知/争议" },
};

export function AnalysesPage({ novelId, onOpenEvidence }: Props) {
  const [charId, setCharId] = useState<string | null>(null);

  const charsQuery = useQuery({
    queryKey: ["analysis-chars", novelId],
    queryFn: () => api.listCharacters(novelId!, { page: 1, pageSize: 50 }),
    enabled: !!novelId,
  });

  const reportsQuery = useQuery({
    queryKey: ["char-analyses", charId],
    queryFn: () => api.getCharacterAnalyses(charId!),
    enabled: !!charId,
  });

  if (!novelId) {
    return <Card>请先在「小说项目」页面导入并选择一部小说。</Card>;
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card
        title={
          <Space>
            <FileSearchOutlined />
            <span>重点人物深度分析报告</span>
          </Space>
        }
        extra={
          <Select
            showSearch
            optionFilterProp="label"
            placeholder="选择人物查看深度报告"
            style={{ width: 240 }}
            value={charId || undefined}
            onChange={setCharId}
            options={(charsQuery.data?.items || []).map((c) => ({
              value: c.id,
              label: `${c.canonical_name} (${c.mention_count}次出场)`,
            }))}
          />
        }
      >
        {!charId && <Empty description="请选择一位人物查看分析报告" />}
        {charId && reportsQuery.data?.length === 0 && (
          <Empty description="该人物尚无深度分析报告，请在任务中心运行 S5_ANALYZE 或 FULL_PIPELINE。" />
        )}
        {(reportsQuery.data || []).map((rep) => (
          <Card
            key={rep.id}
            type="inner"
            style={{ marginBottom: 16 }}
            title={`报告版本 v${rep.generation_version} (${rep.report_type})`}
            extra={
              <Space>
                {rep.model_id && <Tag color="geekblue">{rep.model_id}</Tag>}
                <Tag>{rep.review_status}</Tag>
              </Space>
            }
          >
            <Descriptions bordered column={1} size="small" style={{ marginBottom: 16 }}>
              {Object.entries(rep.content_json || {}).map(([k, v]) => (
                <Descriptions.Item key={k} label={k}>
                  {String(v)}
                </Descriptions.Item>
              ))}
            </Descriptions>

            <Typography.Title level={5}>结论核验清单（事实 / 推断分离）</Typography.Title>
            <List
              size="small"
              dataSource={rep.claims}
              renderItem={(c) => {
                const meta = CLAIM_TAG[c.claim_type] || CLAIM_TAG.unknown;
                return (
                  <List.Item
                    actions={c.evidence_ids.map((eid) => (
                      <Button key={eid} size="small" type="link" onClick={() => onOpenEvidence(eid)}>
                        查看原文证据
                      </Button>
                    ))}
                  >
                    <Space>
                      <Tag color={meta.color}>{meta.label}</Tag>
                      <span>{c.claim_text}</span>
                    </Space>
                  </List.Item>
                );
              }}
            />
          </Card>
        ))}
      </Card>
    </Space>
  );
}
