import { useQuery } from "@tanstack/react-query";
import { Descriptions, Drawer, Spin, Tag, Typography } from "antd";
import { api } from "../../shared/api/client";

const { Paragraph, Text } = Typography;

interface Props {
  evidenceId: string | null;
  onClose: () => void;
}

const STATUS_COLOR: Record<string, string> = {
  verified: "green",
  pending: "gold",
  ambiguous: "orange",
  mismatch: "red",
  out_of_range: "red",
};

export function EvidenceDrawer({ evidenceId, onClose }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ["evidence", evidenceId],
    queryFn: () => api.getEvidence(evidenceId!),
    enabled: !!evidenceId,
  });

  const renderHighlightedContext = () => {
    if (!data) return null;
    const text = data.context_text;
    const start = data.context_utf16_start;
    const end = data.context_utf16_end;
    const before = text.slice(0, start);
    const match = text.slice(start, end);
    const after = text.slice(end);
    return (
      <Paragraph
        style={{
          whiteSpace: "pre-wrap",
          background: "#fafafa",
          padding: 16,
          borderRadius: 6,
          border: "1px solid #f0f0f0",
          lineHeight: 1.8,
        }}
      >
        {before}
        <mark style={{ background: "#ffe58f", padding: "0 2px", fontWeight: 600 }}>{match}</mark>
        {after}
      </Paragraph>
    );
  };

  return (
    <Drawer
      title="原文证据核验"
      width={600}
      open={!!evidenceId}
      onClose={onClose}
      destroyOnClose
    >
      {isLoading && <Spin />}
      {data && (
        <>
          <Descriptions column={1} bordered size="small" style={{ marginBottom: 16 }}>
            <Descriptions.Item label="所属章节">
              第 {data.chapter_no} 章 — {data.chapter_title}
            </Descriptions.Item>
            <Descriptions.Item label="核验状态">
              <Tag color={STATUS_COLOR[data.verification_status] || "default"}>
                {data.verification_status}
              </Tag>
              {data.verification_note && <Text type="secondary">{data.verification_note}</Text>}
            </Descriptions.Item>
            <Descriptions.Item label="字符跨度 (Unicode / UTF-16)">
              [{data.start_offset}, {data.end_offset}) / UTF-16 [{data.utf16_start_offset},{" "}
              {data.utf16_end_offset})
            </Descriptions.Item>
            <Descriptions.Item label="引用原文">
              <Text strong>{data.quote_text}</Text>
            </Descriptions.Item>
          </Descriptions>
          <Typography.Title level={5}>章节上下文定位</Typography.Title>
          {renderHighlightedContext()}
        </>
      )}
    </Drawer>
  );
}
