import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Form, Input, Modal, Select, Space, Table, Tag, message } from "antd";
import { AuditOutlined, MergeCellsOutlined, SplitCellsOutlined } from "@ant-design/icons";
import { api } from "../../shared/api/client";
import type { ReviewTask } from "../../shared/types/api";

interface Props {
  novelId: string | null;
}

export function ReviewsPage({ novelId }: Props) {
  const qc = useQueryClient();
  const [mergeOpen, setMergeOpen] = useState(false);
  const [splitOpen, setSplitOpen] = useState(false);
  const [mergeForm] = Form.useForm();
  const [splitForm] = Form.useForm();

  const reviewsQuery = useQuery({
    queryKey: ["reviews", novelId],
    queryFn: () => api.listReviews(novelId!),
    enabled: !!novelId,
  });

  const charsQuery = useQuery({
    queryKey: ["review-chars", novelId],
    queryFn: () => api.listCharacters(novelId!, { page: 1, pageSize: 100 }),
    enabled: !!novelId,
  });

  const decideMut = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" | "resolve" }) =>
      api.submitReviewDecision(id, decision),
    onSuccess: () => {
      message.success("审核决议已提交并记录审计");
      qc.invalidateQueries({ queryKey: ["reviews", novelId] });
      qc.invalidateQueries({ queryKey: ["characters", novelId] });
    },
    onError: (err: Error) => message.error(err.message),
  });

  const mergeMut = useMutation({
    mutationFn: (v: { source_id: string; target_id: string; reason: string }) =>
      api.mergeCharacters(v.source_id, v.target_id, v.reason),
    onSuccess: () => {
      message.success("人物已合并（保留可撤销审计日志）");
      setMergeOpen(false);
      mergeForm.resetFields();
      qc.invalidateQueries({ queryKey: ["characters", novelId] });
      qc.invalidateQueries({ queryKey: ["review-chars", novelId] });
    },
    onError: (err: Error) => message.error(err.message),
  });

  const splitMut = useMutation({
    mutationFn: (v: { source_id: string; new_name: string; reason: string }) =>
      api.splitCharacter(v.source_id, v.new_name, v.reason),
    onSuccess: () => {
      message.success("人物已拆分并记录审计日志");
      setSplitOpen(false);
      splitForm.resetFields();
      qc.invalidateQueries({ queryKey: ["characters", novelId] });
      qc.invalidateQueries({ queryKey: ["review-chars", novelId] });
    },
    onError: (err: Error) => message.error(err.message),
  });

  if (!novelId) {
    return <Card>请先在「小说项目」页面导入并选择一部小说。</Card>;
  }

  const charOptions = (charsQuery.data?.items || []).map((c) => ({
    value: c.id,
    label: `${c.canonical_name} (${c.status})`,
  }));

  return (
    <>
      <Card
        title={
          <Space>
            <AuditOutlined />
            <span>身份审核工作台</span>
          </Space>
        }
        extra={
          <Space>
            <Button icon={<MergeCellsOutlined />} onClick={() => setMergeOpen(true)}>
              合并人物
            </Button>
            <Button icon={<SplitCellsOutlined />} onClick={() => setSplitOpen(true)}>
              拆分人物
            </Button>
          </Space>
        }
      >
        <Table
          rowKey="id"
          loading={reviewsQuery.isLoading}
          dataSource={reviewsQuery.data?.items || []}
          columns={[
            {
              title: "类型",
              dataIndex: "review_type",
              width: 150,
              render: (v: string) => <Tag color="orange">{v}</Tag>,
            },
            {
              title: "严重级别",
              dataIndex: "severity",
              width: 100,
              render: (v: string) => <Tag color={v === "high" ? "red" : "blue"}>{v}</Tag>,
            },
            { title: "摘要说明", dataIndex: "summary" },
            {
              title: "状态",
              dataIndex: "state",
              width: 100,
              render: (s: string) => <Tag>{s}</Tag>,
            },
            {
              title: "操作",
              width: 180,
              render: (_, rec: ReviewTask) =>
                rec.state === "open" ? (
                  <Space>
                    <Button
                      size="small"
                      type="primary"
                      onClick={() => decideMut.mutate({ id: rec.id, decision: "approve" })}
                    >
                      通过
                    </Button>
                    <Button
                      size="small"
                      danger
                      onClick={() => decideMut.mutate({ id: rec.id, decision: "reject" })}
                    >
                      驳回
                    </Button>
                  </Space>
                ) : (
                  <Tag color="green">已处理</Tag>
                ),
            },
          ]}
        />
      </Card>

      <Modal
        title="合并人物实体（可审计、不物理删除旧 ID）"
        open={mergeOpen}
        onCancel={() => setMergeOpen(false)}
        onOk={() => mergeForm.submit()}
        confirmLoading={mergeMut.isPending}
      >
        <Form form={mergeForm} layout="vertical" onFinish={(v) => mergeMut.mutate(v)}>
          <Form.Item name="source_id" label="被合并人物（将标记为 superseded）" rules={[{ required: true }]}>
            <Select showSearch optionFilterProp="label" options={charOptions} />
          </Form.Item>
          <Form.Item name="target_id" label="目标主人物" rules={[{ required: true }]}>
            <Select showSearch optionFilterProp="label" options={charOptions} />
          </Form.Item>
          <Form.Item name="reason" label="合并依据" rules={[{ required: true }]}>
            <Input.TextArea rows={3} placeholder="说明合并理由与章节依据" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="拆分人物实体"
        open={splitOpen}
        onCancel={() => setSplitOpen(false)}
        onOk={() => splitForm.submit()}
        confirmLoading={splitMut.isPending}
      >
        <Form form={splitForm} layout="vertical" onFinish={(v) => splitMut.mutate(v)}>
          <Form.Item name="source_id" label="原人物实体" rules={[{ required: true }]}>
            <Select showSearch optionFilterProp="label" options={charOptions} />
          </Form.Item>
          <Form.Item name="new_name" label="新拆分人物标准名" rules={[{ required: true }]}>
            <Input placeholder="例如：同名人物 B 或某化身" />
          </Form.Item>
          <Form.Item name="reason" label="拆分依据" rules={[{ required: true }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
