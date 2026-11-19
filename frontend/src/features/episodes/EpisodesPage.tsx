import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  message,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { CheckCircleOutlined, EditOutlined, PlusOutlined } from "@ant-design/icons";
import { api } from "../../shared/api/client";
import type { Episode } from "../../shared/types/api";

interface Props {
  novelId: string | null;
  onSelectEpisodeForScript?: (episodeId: string) => void;
}

export function EpisodesPage({ novelId, onSelectEpisodeForScript }: Props) {
  const qc = useQueryClient();
  const [targetMinutes, setTargetMinutes] = useState(25);
  const [charsPerMinute, setCharsPerMinute] = useState(260);
  const [strategy, setStrategy] = useState<"chapter_first" | "story_first">("chapter_first");
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const [editingEp, setEditingEp] = useState<Episode | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editRationale, setEditRationale] = useState("");

  const plansQuery = useQuery({
    queryKey: ["episode-plans", novelId],
    queryFn: () => api.listEpisodePlans(novelId!),
    enabled: !!novelId,
  });

  const activePlanId = selectedPlanId || plansQuery.data?.[0]?.id || null;
  const activePlan = plansQuery.data?.find((p) => p.id === activePlanId) || null;

  const episodesQuery = useQuery({
    queryKey: ["episodes", activePlanId],
    queryFn: () => api.listEpisodes(activePlanId!),
    enabled: !!activePlanId,
  });

  const createPlanMut = useMutation({
    mutationFn: () =>
      api.createEpisodePlan(novelId!, {
        target_minutes: targetMinutes,
        chars_per_minute: charsPerMinute,
        strategy,
      }),
    onSuccess: (plan) => {
      message.success(`已生成分集方案 v${plan.plan_version}`);
      setSelectedPlanId(plan.id);
      qc.invalidateQueries({ queryKey: ["episode-plans", novelId] });
      qc.invalidateQueries({ queryKey: ["episodes", plan.id] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  const validateMut = useMutation({
    mutationFn: () => api.validateEpisodePlan(activePlanId!),
    onSuccess: (res) => {
      if (res.valid) {
        message.success(`覆盖率校验通过：${(res.coverage_ratio * 100).toFixed(1)}%`);
      } else {
        message.warning(`存在 ${res.errors.length} 个边界问题`);
      }
      qc.invalidateQueries({ queryKey: ["episode-plans", novelId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  const updateEpMut = useMutation({
    mutationFn: () =>
      api.updateEpisode(editingEp!.id, {
        title: editTitle,
        rationale: editRationale,
      }),
    onSuccess: () => {
      message.success("剧集信息已更新");
      setEditingEp(null);
      qc.invalidateQueries({ queryKey: ["episodes", activePlanId] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  if (!novelId) {
    return <Empty description="请先在「小说项目」中选择一本小说" />;
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title="智能分集规划 (EP-01)">
        <Space wrap>
          <span>目标时长(分钟)：</span>
          <InputNumber min={5} max={120} value={targetMinutes} onChange={(v) => setTargetMinutes(v || 25)} />
          <span>语速估算(字/分)：</span>
          <InputNumber min={100} max={600} value={charsPerMinute} onChange={(v) => setCharsPerMinute(v || 260)} />
          <span>分集策略：</span>
          <Select
            style={{ width: 160 }}
            value={strategy}
            onChange={setStrategy}
            options={[
              { value: "chapter_first", label: "章节优先" },
              { value: "story_first", label: "剧情时长优先" },
            ]}
          />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            loading={createPlanMut.isPending}
            onClick={() => createPlanMut.mutate()}
          >
            自动规划分集
          </Button>
          {activePlanId && (
            <Button
              icon={<CheckCircleOutlined />}
              loading={validateMut.isPending}
              onClick={() => validateMut.mutate()}
            >
              校验原文覆盖率
            </Button>
          )}
        </Space>
      </Card>

      {activePlan && (
        <Alert
          type={activePlan.validation_json?.valid ? "success" : "warning"}
          showIcon
          message={`当前方案 v${activePlan.plan_version} | 状态：${activePlan.status} | 原文覆盖率：${(
            (activePlan.validation_json?.coverage_ratio ?? 0) * 100
          ).toFixed(1)}% (${activePlan.validation_json?.covered_chars ?? 0}/${
            activePlan.validation_json?.total_chars ?? 0
          } 字)`}
          description={
            activePlan.validation_json?.errors?.length
              ? activePlan.validation_json.errors.join("；")
              : "所有剧集区间连续、无重叠、100% 忠于规范化原文。"
          }
        />
      )}

      <Card
        title="剧集列表"
        extra={
          plansQuery.data && plansQuery.data.length > 0 ? (
            <Select
              style={{ width: 220 }}
              value={activePlanId || undefined}
              onChange={setSelectedPlanId}
              options={plansQuery.data.map((p) => ({
                value: p.id,
                label: `方案 v${p.plan_version} (${p.target_minutes}分钟 / ${p.status})`,
              }))}
            />
          ) : null
        }
      >
        <Table
          rowKey="id"
          loading={episodesQuery.isLoading}
          dataSource={episodesQuery.data || []}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: "集号", dataIndex: "episode_no", width: 70 },
            { title: "剧集标题", dataIndex: "title" },
            {
              title: "字数 / 段落",
              render: (_, r: Episode) => `${r.char_count} 字 / ${r.paragraph_count} 段`,
            },
            {
              title: "预计时长",
              render: (_, r: Episode) => `${Math.ceil(r.estimated_seconds / 60)} 分钟`,
            },
            {
              title: "状态",
              dataIndex: "status",
              render: (st: string) => <Tag color={st === "audio_ready" ? "green" : "blue"}>{st}</Tag>,
            },
            {
              title: "操作",
              render: (_, r: Episode) => (
                <Space>
                  <Button
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => {
                      setEditingEp(r);
                      setEditTitle(r.title);
                      setEditRationale(r.rationale || "");
                    }}
                  >
                    编辑
                  </Button>
                  {onSelectEpisodeForScript && (
                    <Button
                      size="small"
                      type="link"
                      onClick={() => onSelectEpisodeForScript(r.id)}
                    >
                      进入配音剧本
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={`编辑第 ${editingEp?.episode_no ?? ""} 集`}
        open={!!editingEp}
        onCancel={() => setEditingEp(null)}
        onOk={() => updateEpMut.mutate()}
        confirmLoading={updateEpMut.isPending}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text>剧集标题</Typography.Text>
          <Input value={editTitle} onChange={(e) => setEditTitle(e.target.value)} />
          <Typography.Text>分集理由</Typography.Text>
          <Input.TextArea rows={3} value={editRationale} onChange={(e) => setEditRationale(e.target.value)} />
        </Space>
      </Modal>
    </Space>
  );
}
